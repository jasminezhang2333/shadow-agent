"""Shadow Agent 后端服务入口（FastAPI）。

对 Android 瘦客户端暴露：
- WS  /client/stream : 收前台屏帧(记忆流) + 麦克风 PCM，转推 realtime-MLLM。
- POST /task         : 下达自然语言任务，启动分身 agent 在虚拟屏执行。
- GET  /task/{id}    : 查任务状态 / 交接边界。
- GET  /memory/search: 调试用，直接查记忆库。

把四层接线：gateway(感知+工具触发) ↔ memory(RAG) ↔ tools(比价/动作) ↔ agent(编排)。
"""
from __future__ import annotations

import asyncio
import base64
import os
import uuid

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from .bridge.adb_bridge import AdbBridge
from .bridge.virtual_display import VirtualDisplayManager
from .gateway.gateway_client import GatewayClient, GatewayConfig
from .memory import video_memory_bridge
from .memory.ingest import MemoryIngestor
from .memory.slice_recorder import SliceRecorder
from .memory.retriever import MemoryRetriever
from .memory.vector_store import MemoryStore
from .tools.comparison_prices import ComparisonPrices
from .tools.jd_provider import JdPriceProvider, MockIphoneProvider
from .tools.langchain_tools import TOOL_DESCRIPTIONS
from .tools.phone_act import PhoneActuator
from .agent.task_agent import TaskAgent

app = FastAPI(title="Shadow Agent Backend")

# ---------- 全局单例（简单起见；生产可换依赖注入） ----------
STORE = MemoryStore(persist_dir=os.getenv("CHROMA_DIR", "./.chroma_memory"))
INGESTOR = MemoryIngestor(STORE)
RETRIEVER = MemoryRetriever(STORE)

# 比价：京东爬虫版始终启用；淘宝客 API 需配置三个环境变量才加入
_providers = [MockIphoneProvider(), JdPriceProvider()]
if all(k in os.environ for k in ["TAOBAO_APP_KEY", "TAOBAO_APP_SECRET", "TAOBAO_ADZONE_ID"]):
    try:
        from .tools.taobao_provider import TaobaoPriceProvider
        _providers.append(TaobaoPriceProvider())
    except Exception as e:
        import logging
        logging.getLogger("uvicorn.error").warning(f"淘宝客 provider 初始化失败，跳过: {e}")
COMPARATOR = ComparisonPrices(providers=_providers)

TASKS: dict[str, dict] = {}

ADB_SERIAL = os.getenv("ADB_SERIAL") or None
BRIDGE = AdbBridge(serial=ADB_SERIAL)
VDM = VirtualDisplayManager(serial=ADB_SERIAL)

# ---------- 记忆写路径：40s 切片 + 后台异步抽取 ----------
# 实时推流只负责把切片丢进队列；真正花钱的 qwen3-omni-flash 抽取由常驻 worker
# 在后台一条条消费，和实时热路径解耦（用户说的「离线模型做异步记忆提取」）。
SLICES_DIR = os.getenv("SHADOW_SLICES_DIR",
                       os.path.join(os.path.dirname(__file__), ".slices"))
SLICE_SECONDS = float(os.getenv("SHADOW_SLICE_SECONDS", "40"))
INGEST_QUEUE: asyncio.Queue[str] = asyncio.Queue()


async def _ingest_worker() -> None:
    """常驻后台：从队列取 mp4，子进程桥到 video-memory 抽取+入库。串行消费，
    避免并发打爆云端配额；失败只记日志不影响推流。"""
    import logging
    logger = logging.getLogger("uvicorn.error")
    while True:
        video_path = await INGEST_QUEUE.get()
        try:
            ok, detail = await asyncio.to_thread(
                video_memory_bridge.ingest_video, video_path)
            if ok:
                logger.info(f"[ingest] ✅ {os.path.basename(video_path)} {detail}")
            else:
                logger.warning(f"[ingest] ❌ {os.path.basename(video_path)} {detail}")
        except Exception as e:
            logger.error(f"[ingest] worker error: {e}")
        finally:
            INGEST_QUEUE.task_done()


@app.on_event("startup")
async def _start_ingest_worker() -> None:
    asyncio.create_task(_ingest_worker())


# ---------- gateway 回调工厂：绑定到具体的 Android ws 连接 ----------
# _on_text / _on_tool_call 原本是全局函数，但 WebSocket 是每连接一个。
# 为了把"理解文本 / 工具调用"推回正确那台手机，这里用工厂函数把当前
# ws 闭包进去，除了原有的落库 / 返回逻辑，再额外经 ws 推一条可视化事件。
async def _safe_push(ws: WebSocket, obj: dict) -> None:
    try:
        await ws.send_json(obj)
    except Exception:
        pass


def _make_tool_call_handler(ws: WebSocket):
    async def _on_tool_call(name: str, params: dict) -> str:
        query = params.get("query", "") or params.get("user_input", "")
        await _safe_push(ws, {"type": "tool_call", "name": name, "query": query})
        if name == "memory_retrieval":
            # 走子进程桥到 D:\video-memory 的 4 层记忆服务（本地 Chroma 库为空，已弃用）
            result = await asyncio.to_thread(video_memory_bridge.retrieve, query)
        elif name == "comparison_prices":
            import json as _json
            compare_out = COMPARATOR.compare(query)
            result = _json.dumps(compare_out["results"], ensure_ascii=False)
        else:
            result = f"未知工具 {name}"
        await _safe_push(ws, {"type": "tool_result", "name": name, "result": result})
        return result
    return _on_tool_call


def _make_text_handler(ws: WebSocket):
    async def _on_text(text_delta: str, is_final: bool) -> None:
        # MLLM 对前台画面的理解文本：推回手机做可视化，final 时再 ingest 成记忆
        if text_delta.strip():
            await _safe_push(ws, {
                "type": "understanding", "text": text_delta, "final": is_final})
        if is_final and text_delta.strip():
            INGESTOR.ingest_description(text_delta, app="foreground")
    return _on_text


def _make_transcript_handler(ws: WebSocket):
    async def _on_transcript(text: str) -> None:
        # ASR 对用户语音的句末识别：推回手机显示“你说”气泡
        if text.strip():
            await _safe_push(ws, {"type": "transcription", "text": text})
    return _on_transcript


# ---------- Android 瘦客户端：流上传 ----------
@app.websocket("/client/stream")
async def client_stream(ws: WebSocket):
    import logging
    logger = logging.getLogger("uvicorn.error")

    logger.info(f"[/client/stream] ===== NEW WebSocket connection from {ws.client} =====")

    try:
        await ws.accept()
        logger.info("[/client/stream] ✅ WebSocket accepted")
    except Exception as e:
        logger.error(f"[/client/stream] ❌ Failed to accept WebSocket: {e}")
        return

    cfg = GatewayConfig(
        http_base=os.getenv("GATEWAY_HTTP", "http://127.0.0.1:8080"),
        # gateway /connect 返回的 ws url 硬编码了旧内网 IP，只覆盖 host:port，
        # path 和 ?session_id= 仍用 gateway 返回值（见 gateway_client._resolve_ws_url）
        ws_override=os.getenv("GATEWAY_WS", "ws://127.0.0.1:8081"),
        session_id=f"shadow-{uuid.uuid4().hex[:8]}",
        instructions=("你基于用户第一人称视角的视频和音频理解其经历；"
                      "当用户提到回忆/刚才看到时调用 memory_retrieval，"
                      "提到价格/比价/购买时调用 comparison_prices。"),
        # 只有用户按「提问」键后说的那轮才当问题，过滤刷视频等背景音
        require_button=True,
    )
    gw = GatewayClient(cfg, TOOL_DESCRIPTIONS,
                       _make_tool_call_handler(ws), _make_text_handler(ws),
                       on_transcript=_make_transcript_handler(ws))
    try:
        logger.info("[/client/stream] calling gw.connect()")
        await gw.connect()
        logger.info("[/client/stream] gw.connect() succeeded")
    except Exception as e:
        logger.error(f"[/client/stream] gateway connect failed: {e}")
        await ws.send_json({"type": "error", "msg": f"gateway 连接失败: {e}"})
        await ws.close()
        return

    recv_task = asyncio.create_task(gw.recv_loop())
    recorder = SliceRecorder(SLICES_DIR, INGEST_QUEUE.put, SLICE_SECONDS)
    try:
        while True:
            data = await ws.receive_json()
            if data.get("type") == "ask":
                # 手机按下「提问」键：放行下一轮语音
                gw.arm()
                logger.info("[/client/stream] 🎙️ 用户按提问键，放行下一轮")
                continue
            audio = data.get("audio")
            video = data.get("video")
            logger.debug(f"[/client/stream] got frame: audio={bool(audio)}, video={bool(video)}")
            audio_bytes = base64.b64decode(audio) if audio else None
            video_bytes = base64.b64decode(video) if video else None
            if video_bytes:
                recorder.add_video(video_bytes)
            if audio_bytes:
                recorder.add_audio(audio_bytes)
            await recorder.maybe_flush()
            await gw.push_media(audio=audio_bytes, video_jpeg=video_bytes)
    except WebSocketDisconnect:
        logger.info("[/client/stream] client disconnected")
    except Exception as e:
        logger.error(f"[/client/stream] error: {e}")
    finally:
        await recorder.flush()  # 断连时把尾巴不足 40s 的也落盘抽记忆
        recv_task.cancel()
        await gw.close()
        logger.info("[/client/stream] cleaned up")


# ---------- 任务：启动分身 agent ----------
class TaskRequest(BaseModel):
    goal: str
    package: str = ""          # 目标 App 包名，如 com.taobao.taobao
    activity: str = ""


def _run_task_blocking(task_id: str, req: TaskRequest) -> None:
    TASKS[task_id] = {"status": "running", "goal": req.goal}
    try:
        disp = VDM.create()
        if req.package:
            BRIDGE.start_app(req.package, disp.display_id, req.activity or None)
        actuator = PhoneActuator(BRIDGE, disp.display_id)
        agent = TaskAgent(actuator)
        state = agent.run(req.goal)
        TASKS[task_id] = {
            "status": "handoff" if state.handoff else
                      ("done" if state.done else "stopped"),
            "goal": req.goal,
            "steps": state.steps,
            "handoff": state.handoff,
            "result": state.result,
        }
    except Exception as e:
        TASKS[task_id] = {"status": "error", "goal": req.goal, "error": str(e)}
    finally:
        VDM.destroy()


@app.post("/task")
async def create_task(req: TaskRequest):
    task_id = uuid.uuid4().hex[:8]
    TASKS[task_id] = {"status": "queued", "goal": req.goal}
    # 阻塞型 adb 操作放线程池，避免卡住事件循环
    asyncio.create_task(asyncio.to_thread(_run_task_blocking, task_id, req))
    return {"task_id": task_id}


@app.get("/task/{task_id}")
async def get_task(task_id: str):
    return TASKS.get(task_id, {"status": "not_found"})


@app.get("/memory/search")
async def memory_search(q: str):
    return RETRIEVER.retrieve(q)
