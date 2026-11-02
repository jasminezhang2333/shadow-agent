import json
import time
import base64
import asyncio
from typing import Optional

from .session import Session, get_event_loop
from .tool_keyword_router import try_fire_keyword_tool, is_round_handled
from ..utils.logger import logger


class GeminiCallback:
    """
    将 Gemini Live API 事件转换为既有 WebSocket 输出协议。
    与 DashScope 的 Callback 输出字段保持一致 (response_id / text_delta / audio_delta / tool_call / is_final)。
    """

    def __init__(self, session: Session):
        self.session = session
        self.full_response = ""
        self.sentence_buffer = ""
        self.speech_start_time: Optional[float] = None
        self.response_id = f"resp_{int(time.time() * 1000)}"
        self.response_active = False
        self.pending_tool_calls = []

        self.STRONG_ENDINGS = set('。.！？!?\n')
        self.WEAK_SPLITTERS = set(',，“”<>;；:： ')
        self.TARGET_CHUNK_SIZE = 10
        self.MAX_CHUNK_SIZE = 15

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
            logger.error(f"[{self.session.session_id}] Gemini 发送错误：{e}")

    # ---- 文本智能切片 ----
    def _flush_buffer(self, is_final=False, force_all=False):
        if not self.sentence_buffer:
            return
        if force_all:
            text_to_send = self.sentence_buffer
            self.sentence_buffer = ""
        else:
            split_pos = -1
            for i, char in enumerate(self.sentence_buffer):
                if char in self.STRONG_ENDINGS:
                    split_pos = i + 1
                    break
            if split_pos == -1 and len(self.sentence_buffer) >= self.TARGET_CHUNK_SIZE:
                start = max(0, self.TARGET_CHUNK_SIZE - 2)
                end = min(len(self.sentence_buffer), self.TARGET_CHUNK_SIZE + 2)
                for i in range(end - 1, start - 1, -1):
                    if self.sentence_buffer[i] in self.WEAK_SPLITTERS:
                        split_pos = i + 1
                        break
            if split_pos == -1 and len(self.sentence_buffer) >= self.MAX_CHUNK_SIZE:
                split_pos = self.TARGET_CHUNK_SIZE
            if split_pos == -1:
                return
            text_to_send = self.sentence_buffer[:split_pos]
            self.sentence_buffer = self.sentence_buffer[split_pos:]

        self._send({
            "type": "response",
            "response_id": self.response_id,
            "text_delta": text_to_send,
            "audio_delta": None,
            "is_final": is_final,
            "tool_call": {},
            "timestamp": int(time.time() * 1000),
        })

    # ---- 主入口：处理 Gemini Live 消息 ----
    def on_message(self, message) -> None:
        try:
            sc = getattr(message, 'server_content', None)
            if sc is not None:
                self._handle_server_content(sc)

            tc = getattr(message, 'tool_call', None)
            if tc is not None:
                self._handle_tool_call(tc)

            if getattr(message, 'setup_complete', None) is not None:
                logger.info(f"[{self.session.session_id}] Gemini setup complete")
        except Exception as e:
            logger.error(f"[{self.session.session_id}] Gemini 回调错误：{e}")

    def _handle_server_content(self, sc):
        # 输入语音转写
        in_tr = getattr(sc, 'input_transcription', None)
        if in_tr and getattr(in_tr, 'text', None):
            transcript = in_tr.text
            logger.info(f"[{self.session.session_id}] 🗣️ 识别：{transcript}")
            self.session.log_user(transcript)
            self._send({
                "type": "transcription",
                "text": transcript,
                "timestamp": int(time.time() * 1000),
            })
            # 硬关键词命中即直接发 tool_call；命中后本轮 LLM 增量全丢
            try_fire_keyword_tool(self.session, transcript)

        # 模型回合
        mt = getattr(sc, 'model_turn', None)
        if mt is not None and getattr(mt, 'parts', None):
            for part in mt.parts:
                text = getattr(part, 'text', None)
                if text:
                    self._on_text_delta(text)

                # 音频 inline_data 主动丢弃：Live 模型只能开 AUDIO modality，
                # 但我们只要文字，依赖 output_audio_transcription 提供字幕。
                # 不再向前端转发 audio_delta，节省带宽 + 避免客户端播放杂音。

        # 输出语音转写：作为唯一的"文本输出"流，逐段推给前端 + 累计写 log_ai
        out_tr = getattr(sc, 'output_transcription', None)
        if out_tr and getattr(out_tr, 'text', None):
            self._on_text_delta(out_tr.text)

        if getattr(sc, 'turn_complete', False):
            self._on_turn_complete()

    def _on_text_delta(self, text: str):
        # 本轮已被硬关键词路由处理：LLM 文本增量全部丢弃
        if is_round_handled(self.session):
            return
        if not self.response_active:
            self.response_active = True
            if self.speech_start_time:
                delay = (time.time() - self.speech_start_time) * 1000
                provider = (self.session.config or {}).get('provider', 'gemini')
                logger.info(
                    f"[latency] session={self.session.session_id} provider={provider} "
                    f"round={self.session.round} ttft={int(delay)}ms"
                )
                self.speech_start_time = None

        self.full_response += text
        self.sentence_buffer += text
        while len(self.sentence_buffer) >= self.TARGET_CHUNK_SIZE:
            prev = len(self.sentence_buffer)
            self._flush_buffer(is_final=False)
            if len(self.sentence_buffer) == prev:
                break

    def _on_turn_complete(self):
        logger.info(f"[{self.session.session_id}] ✅ Gemini 响应完成: {self.full_response}")
        # 本轮已被硬关键词路由处理：路由已经 send 过 is_final + 递增过 chat_index
        if is_round_handled(self.session):
            logger.info(
                f"[{self.session.session_id}] 🔑 本轮已由关键词路由处理，"
                f"丢弃 Gemini 完整回复"
            )
            self.full_response = ""
            self.sentence_buffer = ""
            self.response_active = False
            self.speech_start_time = None
            self.response_id = f"resp_{int(time.time() * 1000)}"
            return
        if self.sentence_buffer:
            self._flush_buffer(is_final=False, force_all=True)
        if self.full_response:
            self.session.log_ai(self.full_response)
        self._send({
            "type": "response",
            "response_id": self.response_id,
            "text_delta": "",
            "audio_delta": None,
            "is_final": True,
            "tool_call": {},
            "timestamp": int(time.time() * 1000),
        })
        self.full_response = ""
        self.sentence_buffer = ""
        self.response_active = False
        self.session.chat_index += 1
        self.speech_start_time = None
        self.response_id = f"resp_{int(time.time() * 1000)}"

    def _handle_tool_call(self, tc):
        # 本轮已被硬关键词路由处理：忽略 LLM 的工具调用，避免双发
        if is_round_handled(self.session):
            logger.info(
                f"[{self.session.session_id}] 🔑 本轮已由关键词路由处理，"
                f"忽略 Gemini tool_call"
            )
            return
        function_calls = getattr(tc, 'function_calls', None) or []
        for fc in function_calls:
            name = getattr(fc, 'name', '')
            args = getattr(fc, 'args', {}) or {}
            call_id = getattr(fc, 'id', '')
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    pass
            tool_call_data = {
                'name': name,
                'arguments': args,
                'id': call_id,
                'trigger': '立即执行',
            }
            logger.info(f"[{self.session.session_id}] 🔧 Gemini 工具调用: {name} args={args}")
            self.session.log_ai(f"[工具调用] {json.dumps(tool_call_data, ensure_ascii=False)}")
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
        self.session.chat_index += 1
        self.response_id = f"resp_{int(time.time() * 1000)}"
