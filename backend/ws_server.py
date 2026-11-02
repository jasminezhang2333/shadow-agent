"""Shadow Agent 后端：纯 WebSocket 服务入口（替代 FastAPI）。

只做一件事：接 Android 瘦客户端的音视频流（WS），转推 realtime-MLLM gateway，
并把 gateway 回来的 text/tool_call 落地到记忆库 / 本地工具。

与旧 FastAPI /client/stream 协议保持一致，便于 Android 端零改动切换：
  收帧 JSON：{"audio": base64_pcm?, "video": base64_jpeg?}
  回传 JSON：{"type": "error"|"status"|"handoff", "msg": ...}

启动：python -m backend.ws_server   （默认 0.0.0.0:8000）
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import uuid

import websockets

from .gateway.gateway_client import GatewayClient, GatewayConfig
from .memory.ingest import MemoryIngestor
from .memory.retriever import MemoryRetriever
from .memory.vector_store import MemoryStore
from .tools.comparison_prices import ComparisonPrices
from .tools.langchain_tools import TOOL_DESCRIPTIONS

logger = logging.getLogger("shadow.ws")

# ---------- 全局单例 ----------
STORE = MemoryStore(persist_dir=os.getenv("CHROMA_DIR", "./.chroma_memory"))
INGESTOR = MemoryIngestor(STORE)
RETRIEVER = MemoryRetriever(STORE)
COMPARATOR = ComparisonPrices()


# ---------- gateway 回调：tool_call 路由 + 文本落库 ----------
async def _on_tool_call(name: str, params: dict) -> str:
    query = params.get("query", "") or params.get("user_input", "")
    if name == "memory_retrieval":
        return RETRIEVER.retrieve(query)["answer"]
    if name == "comparison_prices":
        return COMPARATOR.compare(query)["summary"]
    return f"未知工具 {name}"


async def _on_text(text_delta: str, is_final: bool) -> None:
    if is_final and text_delta.strip():
        INGESTOR.ingest_description(text_delta, app="foreground")


# ---------- 每条连接：建 gateway 会话并双向中继 ----------
async def _handle(ws) -> None:
    peer = getattr(ws, "remote_address", None)
    logger.info("new client %s", peer)
    cfg = GatewayConfig(
        http_base=os.getenv("GATEWAY_HTTP", "http://127.0.0.1:8080"),
        ws_override=os.getenv("GATEWAY_WS", "ws://127.0.0.1:8081"),
        session_id=f"shadow-{uuid.uuid4().hex[:8]}",
        instructions=(
            "你基于用户第一人称视角的视频和音频理解其经历；"
            "当用户提到回忆/刚才看到时调用 memory_retrieval，"
            "提到价格/比价/购买时调用 comparison_prices。\n"
            "调用工具时必须严格按以下格式输出JSON（不要加任何XML标签或额外文字）：\n"
            "{'name': 'memory_retrieval', 'arguments': {'query': '用户原话中的关键词'}}\n"
            "{'name': 'comparison_prices', 'arguments': {'query': '用户说的产品名'}}"
        ),
    )
    gw = GatewayClient(cfg, TOOL_DESCRIPTIONS, _on_tool_call, _on_text)
    try:
        await gw.connect()
    except Exception as e:
        logger.error("gateway connect failed: %s", e)
        await _safe_send(ws, {"type": "error", "msg": f"gateway 连接失败: {e}"})
        return

    recv_task = asyncio.create_task(gw.recv_loop())
    try:
        async for raw in ws:
            data = json.loads(raw)
            audio = data.get("audio")
            video = data.get("video")
            await gw.push_media(
                audio=base64.b64decode(audio) if audio else None,
                video_jpeg=base64.b64decode(video) if video else None)
    except websockets.ConnectionClosed:
        logger.info("client %s disconnected", peer)
    except Exception as e:
        logger.error("relay error: %s", e)
    finally:
        recv_task.cancel()
        await gw.close()
        logger.info("cleaned up %s", peer)


async def _safe_send(ws, obj: dict) -> None:
    try:
        await ws.send(json.dumps(obj))
    except Exception:
        pass


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    host = os.getenv("WS_HOST", "0.0.0.0")
    port = int(os.getenv("WS_PORT", "8000"))
    logger.info("shadow ws server on %s:%s", host, port)
    async with websockets.serve(_handle, host, port, max_size=10 * 1024 * 1024):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
