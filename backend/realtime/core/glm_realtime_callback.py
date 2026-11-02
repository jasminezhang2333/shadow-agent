import json
import time
import asyncio
import base64
from typing import Any, Optional

from .session import Session, get_event_loop
from .tool_keyword_router import try_fire_keyword_tool, is_round_handled
from ..utils.logger import logger


class GLMRealtimeCallback:
    """
    解析 GLM-Realtime WebSocket 服务端事件，转成项目统一的 WS 输出协议
    （type=response, text_delta / audio_delta / tool_call / is_final）。

    GLM video_passive 模式下服务端只回 audio token（response.text 永远空），
    本地复用 _GummyAsrClient 把 audio.delta 的 PCM 转成中文文本，作为 LLM
    的回复 text_delta 转给前端。
    """

    def __init__(self, session: Session):
        self.session = session
        self.full_response = ""
        self.sentence_buffer = ""
        self.response_id = f"resp_{int(time.time() * 1000)}"
        # 同一轮里如果 GLM 同时下发 text.delta 和 audio_transcript.delta，
        # 两路文本是重复的——只采用本轮最先出现的一路，避免每个字都翻倍。
        self._text_source: Optional[str] = None
        # 排障期间打印前 N 条原始事件，定位卡死位置
        self._raw_evt_logged = 0
        self._raw_evt_log_max = 20

        # 标点切片：累积流式文本，遇到任意标点（强或弱）就切一段送出。
        # 不再设字符数上限/下限——若整段没标点会等到结束 force_all 一次性发。
        self.STRONG_ENDINGS = set('。.！？!?\n')
        self.WEAK_SPLITTERS = set(',，""<>;；:：')
        self.SPLIT_CHARS = self.STRONG_ENDINGS | self.WEAK_SPLITTERS

        # 本轮的 audio→text ASR 实例（每个 response 一个，response.done 关闭）
        # GLM e2e 输出 24kHz PCM
        self._asr_client = None
        self._asr_sample_rate = 24000

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
            logger.error(f"[{self.session.session_id}] GLM 发送错误：{e}")

    def _flush_buffer(self, is_final=False, force_all=False, audio_b64: str = ""):
        if not self.sentence_buffer and not audio_b64:
            return
        if force_all:
            text_to_send = self.sentence_buffer
            self.sentence_buffer = ""
        else:
            text_to_send = ""
            split_pos = -1
            for i, ch in enumerate(self.sentence_buffer):
                if ch in self.SPLIT_CHARS:
                    split_pos = i + 1
                    break
            if split_pos != -1:
                text_to_send = self.sentence_buffer[:split_pos]
                self.sentence_buffer = self.sentence_buffer[split_pos:]

        if not text_to_send and not audio_b64:
            return

        self._send({
            "type": "response",
            "response_id": self.response_id,
            "text_delta": text_to_send,
            "audio_delta": audio_b64 or None,
            "is_final": is_final,
            "tool_call": {},
            "timestamp": int(time.time() * 1000),
        })

    def on_message(self, raw: str):
        try:
            evt = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            logger.error(f"[{self.session.session_id}] GLM 解析事件失败: {e}; raw={raw[:200]}")
            return

        etype = evt.get("type")
        if etype == "heartbeat":
            return

        if self._raw_evt_logged < self._raw_evt_log_max:
            self._raw_evt_logged += 1
            logger.info(
                f"[{self.session.session_id}] GLM raw evt #{self._raw_evt_logged}: "
                f"{json.dumps(evt, ensure_ascii=False)[:500]}"
            )

        if etype == "session.updated" or etype == "session.update":
            sess = evt.get("session", {}) or {}
            beta = sess.get("beta_fields", {}) or {}
            logger.info(
                f"[{self.session.session_id}] GLM 会话已更新 "
                f"modalities={sess.get('modalities')} "
                f"input_audio_format={sess.get('input_audio_format')} "
                f"turn_detection={sess.get('turn_detection')} "
                f"chat_mode={beta.get('chat_mode')} "
                f"tts_source={beta.get('tts_source')}"
            )
            return

        if etype == "conversation.item.input_audio_transcription.completed":
            text = evt.get("transcript", "") or ""
            if text:
                logger.info(f"[{self.session.session_id}] 🎙️ 用户语音转写: {text}")
                self.session.log_user(text)
                # 硬关键词命中即直接发 tool_call；命中后本轮 LLM 增量全丢
                try_fire_keyword_tool(self.session, text)
            return

        if etype == "response.created":
            rid = (evt.get("response") or {}).get("id") or f"resp_{int(time.time() * 1000)}"
            self.response_id = rid
            self.full_response = ""
            self.sentence_buffer = ""
            self._text_source = None
            self._turn_start_ts = time.time()
            self._first_token_logged = False
            return

        if etype in ("response.audio_transcript.delta", "response.text.delta"):
            # 本轮已被硬关键词路由处理：LLM 增量全部丢弃
            if is_round_handled(self.session):
                return
            # 同一轮里 GLM 可能两路都发，内容重复——锁定先到的那路
            if self._text_source is None:
                self._text_source = etype
            elif etype != self._text_source:
                return
            delta = evt.get("delta", "") or ""
            if delta:
                if not getattr(self, '_first_token_logged', False):
                    self._first_token_logged = True
                    ttft_ms = int((time.time() - getattr(self, '_turn_start_ts', time.time())) * 1000)
                    provider = (self.session.config or {}).get('provider', 'glm')
                    logger.info(
                        f"[latency] session={self.session.session_id} provider={provider} "
                        f"round={self.session.round} ttft={ttft_ms}ms"
                    )
                self.full_response += delta
                self.sentence_buffer += delta
                # 只要 buffer 里还有标点就一直切
                while any(ch in self.SPLIT_CHARS for ch in self.sentence_buffer):
                    prev = len(self.sentence_buffer)
                    self._flush_buffer(is_final=False)
                    if len(self.sentence_buffer) == prev:
                        break
            return

        if etype == "response.audio.delta":
            if is_round_handled(self.session):
                return
            audio = evt.get("delta", "") or ""
            if audio:
                self._send({
                    "type": "response",
                    "response_id": self.response_id,
                    "text_delta": "",
                    "audio_delta": audio,
                    "is_final": False,
                    "tool_call": {},
                    "timestamp": int(time.time() * 1000),
                })
            return

        if etype in ("response.audio_transcript.done", "response.text.done", "response.audio.done"):
            return

        if etype == "response.function_call_arguments.done":
            # 本轮已被硬关键词路由处理：忽略 LLM 的工具调用，避免双发
            if is_round_handled(self.session):
                logger.info(
                    f"[{self.session.session_id}] 🔑 本轮已由关键词路由处理，"
                    f"忽略 GLM function_call_arguments.done"
                )
                return
            name = evt.get("name", "")
            args_str = evt.get("arguments", "") or ""
            try:
                args_obj = json.loads(args_str) if args_str else {}
            except Exception:
                args_obj = {"_raw": args_str}
            tool_call_data = {
                "name": name,
                "arguments": args_obj,
                "id": evt.get("event_id", ""),
                "trigger": "立即执行",
            }
            logger.info(f"[{self.session.session_id}] 🔧 GLM 工具调用: {name} args={args_obj}")
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
            return

        if etype == "response.done":
            # 本轮已被硬关键词路由处理：路由已经发过 is_final 并递增过 chat_index，
            # 这里只清状态，不再重发也不再递增
            if is_round_handled(self.session):
                logger.info(
                    f"[{self.session.session_id}] 🔑 本轮已由关键词路由处理，"
                    f"丢弃 GLM 完整回复"
                )
                self.full_response = ""
                self.sentence_buffer = ""
                self.response_id = f"resp_{int(time.time() * 1000)}"
                return
            if self.sentence_buffer:
                self._flush_buffer(is_final=False, force_all=True)
            if self.full_response:
                logger.info(f"[{self.session.session_id}] ✅ GLM 响应完成: {self.full_response}")
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
            self.session.chat_index += 1
            self.response_id = f"resp_{int(time.time() * 1000)}"
            return

        if etype == "error":
            err = evt.get("error", {}) or {}
            msg = err.get("message", str(err))
            logger.error(f"[{self.session.session_id}] GLM error: {msg}")
            self._send({
                "type": "error",
                "message": msg,
                "timestamp": int(time.time() * 1000),
            })
            return

        if etype in (
            "session.created",
            "input_audio_buffer.speech_started",
            "input_audio_buffer.speech_stopped",
            "input_audio_buffer.committed",
            "input_audio_buffer.cleared",
            "conversation.created",
            "conversation.item.created",
            "conversation.item.input_audio_transcription.failed",
            "rate_limits.updated",
            "response.output_item.added",
            "response.content_part.added",
            "response.content_part.done",
            "response.output_item.done",
            "response.function_call.simple_browser",
        ):
            logger.info(f"[{self.session.session_id}] GLM evt: {etype}")
            return

        logger.warning(f"[{self.session.session_id}] GLM 未处理事件: {etype} payload={json.dumps(evt, ensure_ascii=False)[:300]}")
