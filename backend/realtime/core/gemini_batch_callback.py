import json
import time
import asyncio
from typing import Dict

from .session import Session, get_event_loop
from ..utils.logger import logger


class GeminiBatchCallback:
    """
    处理 OpenAI 兼容端点 (chat.completions stream) 的 delta，
    转换为既有 WebSocket 输出协议（text_delta / tool_call / is_final）。
    audio_delta 恒为 null（非 Live 模型不返回音频）。
    """

    def __init__(self, session: Session):
        self.session = session
        self.full_response = ""
        self.sentence_buffer = ""
        self.response_id = f"resp_{int(time.time() * 1000)}"
        self._tool_calls_acc: Dict[int, dict] = {}
        # 日志标签跟随当前 provider，避免豆包链路里出现 "GeminiBatch" 误导
        prov = (session.config or {}).get('provider') or 'batch'
        self._log_tag = {
            'doubao_rtc': 'DoubaoBatch',
            'gemini': 'GeminiBatch',
        }.get(prov, prov)

        self.STRONG_ENDINGS = set('。.！？!?\n')
        self.WEAK_SPLITTERS = set(',，""<>;；:： ')
        self.TARGET_CHUNK_SIZE = 10
        self.MAX_CHUNK_SIZE = 15

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
            logger.error(f"[{self.session.session_id}] {self._log_tag} 发送错误：{e}")

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

    def on_turn_start(self):
        self.full_response = ""
        self.sentence_buffer = ""
        self.response_id = f"resp_{int(time.time() * 1000)}"
        self._tool_calls_acc = {}
        self._turn_start_ts = time.time()
        self._first_token_logged = False

    def on_chunk(self, chunk) -> str:
        """处理一个 OpenAI stream chunk，返回本块纯文本（供 provider 累计 history）。"""
        chunk_text = ""
        try:
            choices = getattr(chunk, 'choices', None)
            if not choices:
                return ""
            delta = getattr(choices[0], 'delta', None)
            if not delta:
                return ""

            content = getattr(delta, 'content', None)
            if content:
                chunk_text += content
                self._on_text_delta(content)

            tool_calls = getattr(delta, 'tool_calls', None) or []
            for tc in tool_calls:
                idx = getattr(tc, 'index', 0) or 0
                slot = self._tool_calls_acc.setdefault(idx, {'name': '', 'arguments': '', 'id': ''})
                if getattr(tc, 'id', None):
                    slot['id'] = tc.id
                fn = getattr(tc, 'function', None)
                if fn:
                    if getattr(fn, 'name', None):
                        slot['name'] = fn.name
                    if getattr(fn, 'arguments', None):
                        slot['arguments'] += fn.arguments
        except Exception as e:
            logger.error(f"[{self.session.session_id}] {self._log_tag} on_chunk 异常: {e}")
        return chunk_text

    def _on_text_delta(self, text: str):
        if not self._first_token_logged:
            self._first_token_logged = True
            ttft_ms = int((time.time() - self._turn_start_ts) * 1000)
            provider = (self.session.config or {}).get('provider', '?')
            logger.info(
                f"[latency] session={self.session.session_id} provider={provider} "
                f"round={self.session.round} ttft={ttft_ms}ms"
            )
        self.full_response += text
        self.sentence_buffer += text
        while len(self.sentence_buffer) >= self.TARGET_CHUNK_SIZE:
            prev = len(self.sentence_buffer)
            self._flush_buffer(is_final=False)
            if len(self.sentence_buffer) == prev:
                break

    def _emit_tool_calls(self):
        if not self._tool_calls_acc:
            return
        for idx in sorted(self._tool_calls_acc.keys()):
            slot = self._tool_calls_acc[idx]
            args = slot['arguments']
            try:
                args_obj = json.loads(args) if args else {}
            except Exception:
                args_obj = {'_raw': args}
            tool_call_data = {
                'name': slot['name'],
                'arguments': args_obj,
                'id': slot['id'],
                'trigger': '立即执行',
            }
            logger.info(f"[{self.session.session_id}] 🔧 {self._log_tag} 工具调用: {slot['name']} args={args_obj}")
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
        self._tool_calls_acc = {}

    def on_turn_complete(self):
        logger.info(f"[{self.session.session_id}] ✅ {self._log_tag} 响应完成: {self.full_response}")
        if self.sentence_buffer:
            self._flush_buffer(is_final=False, force_all=True)

        had_tools = bool(self._tool_calls_acc)
        self._emit_tool_calls()

        if self.full_response:
            self.session.log_ai(self.full_response)

        if not had_tools:
            self._send({
                "type": "response",
                "response_id": self.response_id,
                "text_delta": "",
                "audio_delta": None,
                "is_final": True,
                "tool_call": {},
                "timestamp": int(time.time() * 1000),
            })
            self.session.chat_index += 1

        self.full_response = ""
        self.sentence_buffer = ""
        self.response_id = f"resp_{int(time.time() * 1000)}"

    def on_error(self, error: Exception):
        logger.error(f"[{self.session.session_id}] {self._log_tag} 推理异常: {error}")
        self._send({
            "type": "error",
            "message": str(error),
            "timestamp": int(time.time() * 1000),
        })
