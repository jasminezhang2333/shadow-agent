"""
DoubaoRtcCallback：处理 helper 通过 stdout 转发的 RTC AI Agent 字幕，
转换为既有 WebSocket 输出协议（text_delta / is_final / tool_call），
跟 dashscope 的 callback 对齐。

Helper 的 SUBTITLE 帧格式为 JSON：
    {
      "uid": "<发送者>",
      "channel": "room_binary" | "room_text" | "subtitle",
      "data": "<base64>"   # 仅 room_binary 有
      "text": "<文本>"     # room_text/subtitle 直出
    }
RTC AI Agent 字幕实测走 channel=room_binary，载荷为 'subv' 包裹的 JSON：
    [4B "subv"][4B big-endian length][JSON 字符串]
JSON 内每个 entry 的 definite/firstCharPos/lastCharPos/text/userId 含义：
- definite=false 中间态（同一句还在累计）
- definite=true 句末，但 *不是* 轮末——一轮回复会被切成多个 definite 句
- firstCharPos 是该句首字在整轮回复中的偏移，用它拼接整轮
- 客户端用 inactivity timer（最近一段后 N 秒没新字幕）判定轮结束
"""
import ast
import base64
import json
import re
import threading
import time
import asyncio
from typing import Dict, Optional

from .session import Session, get_event_loop
from .callback import _looks_like_tool_call_start, _parse_tool_call_text
from .tool_keyword_router import try_fire_keyword_tool, is_round_handled
from ..utils.logger import logger


# 一轮回复结束判定：最近一次 definite 段后多少秒没有新字幕 → 视为轮结束
TURN_INACTIVITY_SEC = 1.5


class DoubaoRtcCallback:
    SUBV_MAGIC = b'subv'

    def __init__(self, session: Session):
        self.session = session
        self.response_id = f"resp_{int(time.time() * 1000)}"
        self._turn_started: bool = False
        self._turn_start_ts: float = 0.0
        self._first_token_logged: bool = False

        # 工具调用模式：跟 dashscope 一样
        self._is_tool_call_mode: bool = False
        # 整轮累计文本（按 firstCharPos 拼）
        self._segments: Dict[int, str] = {}
        # 已发给 ws 的字符长度（取增量用）
        self._sent_len: int = 0
        # inactivity timer
        self._idle_timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

        rtc_info = (session.config or {}).get('doubao_rtc') or {}
        self._agent_uid = rtc_info.get('agent_user_id') or ''

    # ---- WebSocket 发送 ----
    def _send(self, data: dict):
        data['chat_index'] = self.session.chat_index
        if not self.session.websocket or self.session.is_closed:
            return
        try:
            loop = getattr(self.session, 'websocket_loop', None) or get_event_loop()
            asyncio.run_coroutine_threadsafe(
                self.session.websocket.send(json.dumps(data, ensure_ascii=False)),
                loop
            )
        except Exception as e:
            logger.error(f"[{self.session.session_id}] DoubaoRtc 发送错误：{e}")

    # ---- helper 入口 ----
    def on_helper_subtitle(self, frame_json_str: str):
        try:
            frame = json.loads(frame_json_str)
        except Exception as e:
            logger.warning(f"[{self.session.session_id}] DoubaoRtc 字幕 JSON 解析失败: {e}")
            return

        uid = frame.get('uid', '')
        channel = frame.get('channel', '')

        if channel == 'room_binary':
            payload_b64 = frame.get('data', '')
            self._on_room_binary(uid, payload_b64)
        elif channel in ('room_text', 'subtitle'):
            text = frame.get('text', '')
            if text:
                self._handle_entry(uid, text=text, definite=True, first_pos=None)

    def _on_room_binary(self, uid: str, payload_b64: str):
        try:
            raw = base64.b64decode(payload_b64)
        except Exception as e:
            logger.warning(f"[{self.session.session_id}] room_binary base64 解码失败: {e}")
            return
        if len(raw) < 8 or raw[:4] != self.SUBV_MAGIC:
            logger.debug(
                f"[{self.session.session_id}] 非 subv room_binary 长度={len(raw)} head={raw[:8].hex()}"
            )
            return
        body_len = int.from_bytes(raw[4:8], 'big')
        body = raw[8:8 + body_len]
        try:
            obj = json.loads(body.decode('utf-8', errors='replace'))
        except Exception as e:
            logger.warning(f"[{self.session.session_id}] subv JSON 解析失败: {e}")
            return

        for entry in obj.get('data', []) or []:
            text = entry.get('text', '') or ''
            if not text:
                continue
            self._handle_entry(
                entry.get('userId', uid) or uid,
                text=text,
                definite=bool(entry.get('definite', False)),
                first_pos=entry.get('firstCharPos'),
            )

    def _handle_entry(self, uid: str, *, text: str, definite: bool, first_pos):
        is_agent = (uid == self._agent_uid)
        if not is_agent:
            # 用户语音 ASR 走 transcription 通道，按句直出
            self._send({
                "type": "transcription",
                "text": text,
                "is_final": definite,
                "timestamp": int(time.time() * 1000),
            })
            if definite:
                self.session.log_user(text)
                # 硬关键词命中即直接发 tool_call；命中后本轮 LLM 增量全丢
                try_fire_keyword_tool(self.session, text)
            return

        # ----- Agent 回复 -----
        # 本轮已被硬关键词路由处理：丢弃所有 Agent 字幕
        if is_round_handled(self.session):
            return
        with self._lock:
            self._cancel_idle_timer_locked()

            if not self._turn_started:
                self._turn_started = True
                self._turn_start_ts = time.time()
                self._first_token_logged = False
                self._is_tool_call_mode = False
                self._segments = {}
                self._sent_len = 0
                self.response_id = f"resp_{int(time.time() * 1000)}"

            # 用 firstCharPos 拼整轮文本；没有 firstCharPos 的退化为追加在末尾
            key = int(first_pos) if first_pos is not None else len(self._segments) * 10000
            self._segments[key] = text
            full = self._build_full_locked()

            if not self._first_token_logged and full:
                self._first_token_logged = True
                ttft_ms = int((time.time() - self._turn_start_ts) * 1000)
                provider = (self.session.config or {}).get('provider', 'doubao_rtc')
                logger.info(
                    f"[latency] session={self.session.session_id} provider={provider} "
                    f"round={self.session.round} ttft={ttft_ms}ms"
                )

            # 工具调用检测：跟 dashscope 同款判定
            if not self._is_tool_call_mode:
                stripped = full.lstrip()
                if _looks_like_tool_call_start(stripped):
                    self._is_tool_call_mode = True
                    logger.info(
                        f"[{self.session.session_id}] 🔧 检测到工具调用模式 "
                        f"(起始特征: {stripped[:20]!r})"
                    )

            if not self._is_tool_call_mode and definite:
                # 普通文本：句末才发增量，避免中间态频繁刷屏
                delta = full[self._sent_len:]
                if delta:
                    self._sent_len = len(full)
                    self._send({
                        "type": "response",
                        "response_id": self.response_id,
                        "text_delta": delta,
                        "audio_delta": None,
                        "is_final": False,
                        "tool_call": {},
                        "timestamp": int(time.time() * 1000),
                    })

            # 不管哪种模式，都用 inactivity timer 判轮结束
            self._idle_timer = threading.Timer(
                TURN_INACTIVITY_SEC, self._on_idle_timeout)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _build_full_locked(self) -> str:
        return ''.join(self._segments[k] for k in sorted(self._segments.keys()))

    def _cancel_idle_timer_locked(self):
        if self._idle_timer is not None:
            try:
                self._idle_timer.cancel()
            except Exception:
                pass
            self._idle_timer = None

    def _on_idle_timeout(self):
        with self._lock:
            if not self._turn_started:
                return
            full = self._build_full_locked()
            self._on_turn_end_locked(full)

    # ---- 轮结束 ----
    def _parse_tool_call(self, raw_content: str) -> Optional[dict]:
        return _parse_tool_call_text(raw_content)

    def _on_turn_end_locked(self, full: str):
        if self._is_tool_call_mode:
            tool_call_data = self._parse_tool_call(full.strip())
            if tool_call_data:
                logger.info(
                    f"[{self.session.session_id}] 🔧 成功解析工具调用: "
                    f"{tool_call_data.get('name')}"
                )
                self.session.log_ai(
                    f"[工具调用] {json.dumps(tool_call_data, ensure_ascii=False)}"
                )
                # image_editing.image_to_edit / image_to_3d.src_image 必须落在
                # image_tracker 列表里；缺失就回退一句话。
                new_tcd, img_err = self.session.image_tracker.apply_to_tool_call(tool_call_data)
                if img_err:
                    self._send({
                        "type": "response",
                        "response_id": self.response_id,
                        "text_delta": img_err,
                        "audio_delta": None,
                        "is_final": True,
                        "tool_call": {},
                        "timestamp": int(time.time() * 1000),
                    })
                else:
                    self._send({
                        "type": "response",
                        "response_id": self.response_id,
                        "text_delta": "",
                        "audio_delta": None,
                        "is_final": True,
                        "tool_call": new_tcd,
                        "timestamp": int(time.time() * 1000),
                    })
            else:
                logger.warning(
                    f"[{self.session.session_id}] ❌ 工具调用解析失败，原文: {full!r}"
                )
                self.session.log_ai(full)
                self._send({
                    "type": "response",
                    "response_id": self.response_id,
                    "text_delta": full,
                    "audio_delta": None,
                    "is_final": True,
                    "tool_call": {},
                    "timestamp": int(time.time() * 1000),
                })
        else:
            logger.info(f"[{self.session.session_id}] ✅ DoubaoRtc 响应完成: {full}")
            self.session.log_ai(full)
            # 还有未发的尾巴（罕见，理论上 sent_len 已等于 len(full)），追上
            tail = full[self._sent_len:]
            self._send({
                "type": "response",
                "response_id": self.response_id,
                "text_delta": tail,
                "audio_delta": None,
                "is_final": True,
                "tool_call": {},
                "timestamp": int(time.time() * 1000),
            })

        self.session.chat_index += 1
        self._turn_started = False
        self._is_tool_call_mode = False
        self._segments = {}
        self._sent_len = 0
        self._cancel_idle_timer_locked()
        self.response_id = f"resp_{int(time.time() * 1000)}"
