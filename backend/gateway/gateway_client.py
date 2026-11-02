"""realtime-MLLM gateway 客户端：本项目作为「Agent 端」接入参考服务。

职责（见 INTERFACE.md）：
1. POST /v1/realtime/connect 建会话，带上 memory_retrieval / comparison_prices
   工具描述和 system instructions。
2. 开 WebSocket :8081 /v1/realtime/stream，持续推用户前台帧 + 语音（media.stream）。
3. 收 response：
   - text_delta：MLLM 对画面的理解 → 既是用户问答，也是记忆描述来源。
   - tool_call{memory_retrieval|comparison_prices}：路由到本地 tools 执行。
4. DELETE 关会话。

注意参考服务里 /connect 返回的 websocket_url 可能硬编码成某个内网 IP
（见其 README 坑 #3），这里允许用 ws_override 覆盖。
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import aiohttp


@dataclass
class GatewayConfig:
    http_base: str = "http://127.0.0.1:8080"
    ws_override: str | None = None  # 只覆盖 ws url 的 host:port（保留 path+?session_id=）
    session_id: str = "shadow-001"
    user_id: str = "user-001"
    instructions: str = ""
    video_fps: int = 2
    # 按键门控：开启后，只有用户按「提问」键之后说出的那一轮语音才算提问，
    # 整轮才显示 + 执行工具；没按键时的语音（刷视频背景音等）整轮丢弃。
    # False = 不门控（旧行为，所有语音都当提问）。
    require_button: bool = False


# tool_call 处理器签名：收到 (name, params) 返回工具执行结果文本
ToolHandler = Callable[[str, dict], Awaitable[str]]
# 文本处理器：收到 MLLM 文本增量（用于记忆 ingest / 用户反馈）
TextHandler = Callable[[str, bool], Awaitable[None]]
# 识别处理器：收到 ASR 对用户语音的句末识别文本
TranscriptHandler = Callable[[str], Awaitable[None]]


class GatewayClient:
    def __init__(self, config: GatewayConfig, tool_descriptions: list[dict],
                 on_tool_call: ToolHandler, on_text: TextHandler | None = None,
                 on_transcript: TranscriptHandler | None = None):
        self.cfg = config
        self.tool_descriptions = tool_descriptions
        self.on_tool_call = on_tool_call
        self.on_text = on_text
        self.on_transcript = on_transcript
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws_url: str = ""
        # 按键门控：_armed 由「提问」键置位；VAD 检测到语音开始时把 _armed
        # 消费成 _round_active（一次按键只放行一轮）。未门控时恒为激活。
        self._gating = config.require_button
        self._armed = False
        self._round_active = not self._gating

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        payload = {
            "session_id": self.cfg.session_id,
            "user_id": self.cfg.user_id,
            "tool_descriptions": self.tool_descriptions,
            "output_modalities": ["text"],
            "instructions": self.cfg.instructions,
            "enable_vad": True,
            "video_fps": self.cfg.video_fps,
        }
        url = f"{self.cfg.http_base}/v1/realtime/connect"
        async with self._session.post(url, json=payload) as resp:
            data = await resp.json()
        if data.get("connection_status") != "connected":
            raise RuntimeError(f"gateway connect failed: {data}")
        self._ws_url = self._resolve_ws_url(data["websocket_url"])
        self._ws = await self._session.ws_connect(self._ws_url)

    def _resolve_ws_url(self, ws_url: str) -> str:
        """gateway 返回的 websocket_url 可能是内网里的旧 IP（见 README 坑 #3）。
        用 ws_override 只替换 scheme+host:port，务必保留原 path 和 ?session_id= query。"""
        if not self.cfg.ws_override:
            return ws_url
        orig = urlsplit(ws_url)
        ovr = urlsplit(self.cfg.ws_override)
        return urlunsplit((
            ovr.scheme or orig.scheme,
            ovr.netloc or orig.netloc,
            orig.path, orig.query, orig.fragment,
        ))

    async def push_media(self, audio: bytes | None = None,
                         video_jpeg: bytes | None = None) -> None:
        if self._ws is None:
            raise RuntimeError("WebSocket 未连接")
        msg = {
            "type": "media.stream",
            "session_id": self.cfg.session_id,
            "timestamp": int(time.time() * 1000),
        }
        if audio:
            msg["audio"] = base64.b64encode(audio).decode()
        if video_jpeg:
            msg["video"] = base64.b64encode(video_jpeg).decode()
        await self._ws.send_str(json.dumps(msg))

    async def recv_loop(self) -> None:
        """持续收 response，把 tool_call 路由给 handler，结果回灌。"""
        if self._ws is None:
            raise RuntimeError("WebSocket 未连接")
        async for msg in self._ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            mtype = data.get("type")
            if mtype == "vad_started":
                # 语音开始：把「是否按过提问键」定格成本轮是否激活，然后清掉按键标志
                if self._gating:
                    self._round_active = self._armed
                    self._armed = False
                continue
            if mtype == "transcription":
                # 未激活轮（没按提问键就说话）= 背景音，整轮丢弃，不显示「你说」气泡
                if self._gating and not self._round_active:
                    continue
                if self.on_transcript is not None:
                    await self.on_transcript(data.get("text", ""))
                continue
            if mtype != "response":
                continue
            await self._dispatch(data)

    def arm(self) -> None:
        """用户按下「提问」键：放行下一轮语音（显示问答 + 执行工具）。"""
        self._armed = True

    async def _dispatch(self, data: dict) -> None:
        # 未激活轮（没按提问键）：理解文本 / 工具调用全部丢弃，不显示也不执行
        if self._gating and not self._round_active:
            return
        tool_call = data.get("tool_call") or {}
        text_delta = data.get("text_delta", "")
        is_final = data.get("is_final", False)

        # 有工具调用时跳过视频理解文本输出
        has_tool_call = tool_call and tool_call.get("name")

        if text_delta and self.on_text is not None and not has_tool_call:
            await self.on_text(text_delta, is_final)

        if has_tool_call:
            name = tool_call["name"]
            # realtime-MLLM 各回调统一发 "arguments"（dict，偶尔是 JSON 字符串）；
            # 兼容老字段名 "params"。
            raw_args = tool_call.get("arguments")
            if raw_args is None:
                raw_args = tool_call.get("params", {})
            if isinstance(raw_args, str):
                try:
                    raw_args = json.loads(raw_args)
                except Exception:
                    raw_args = {}
            params = raw_args or {}
            result = await self.on_tool_call(name, params)
            await self._send_tool_result(name, result)

    async def _send_tool_result(self, name: str, result: str) -> None:
        """把工具结果通过文本通道回灌给 MLLM，让它据此生成最终答复。"""
        if self._session is None:
            return
        url = f"{self.cfg.http_base}/v1/realtime/text"
        payload = {
            "session_id": self.cfg.session_id,
            "text": f"[工具 {name} 返回]{result}",
            "stream": False,
        }
        try:
            async with self._session.post(url, json=payload):
                pass
        except Exception:
            pass

    async def close(self) -> None:
        try:
            if self._ws is not None:
                await self._ws.send_str(json.dumps(
                    {"type": "close", "session_id": self.cfg.session_id}))
                await self._ws.close()
        finally:
            if self._session is not None:
                try:
                    await self._session.delete(
                        f"{self.cfg.http_base}/v1/realtime/session/"
                        f"{self.cfg.session_id}")
                except Exception:
                    pass
                await self._session.close()
