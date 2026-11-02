"""
Provider 抽象层：统一 DashScope (Qwen-Omni Realtime) 与 Gemini Live API 的接入。
对外暴露同一组方法 connect / append_audio / append_video / close，并支持判活与重连。
"""
import asyncio
import audioop
import base64
import io
import json
import os
import struct
import threading
import time
from typing import Optional, List

from ..config import Settings
from ..utils.logger import logger
from .session import Session
from .tool_keyword_router import try_fire_keyword_tool


class BaseProvider:
    name = "base"

    def __init__(self, session: Session):
        self.session = session

    def connect(self): ...
    def append_audio(self, audio_b64: str): ...
    def append_video(self, video_b64: str): ...
    def close(self): ...
    def is_alive(self) -> bool:
        return True

    # 默认透传：返回原始 base64。各 provider 可 override 做大小护栏 / 重编码。
    # 返回 None 表示该帧应被丢弃。
    def preprocess_video_b64(self, video_b64: str) -> Optional[str]:
        return video_b64


# ============== DashScope ==============
class DashScopeProvider(BaseProvider):
    name = "dashscope"

    def __init__(self, session: Session):
        super().__init__(session)
        from .callback import Callback
        from dashscope.audio.qwen_omni import OmniRealtimeConversation, MultiModality, AudioFormat
        import dashscope

        self._OmniRealtimeConversation = OmniRealtimeConversation
        self._MultiModality = MultiModality
        self._AudioFormat = AudioFormat
        self._dashscope = dashscope
        self._Callback = Callback

    def connect(self):
        # 优先用 session 上分配的 key；缺省回退到主 key。
        # 显式传给构造函数避免多并发会话覆盖 dashscope.api_key 全局变量。
        api_key = getattr(self.session, 'dashscope_api_key', None) or Settings.DASHSCOPE_API_KEY
        callback = self._Callback(self.session)
        self.session.callback = callback

        conversation = self._OmniRealtimeConversation(
            model=Settings.MODEL_NAME,
            callback=callback,
            api_key=api_key,
        )
        conversation.connect()
        modalities = [self._MultiModality.TEXT, self._MultiModality.AUDIO]
        conversation.update_session(
            output_modalities=modalities,
            voice=self.session.config['voice'],
            input_audio_format=self._AudioFormat.PCM_16000HZ_MONO_16BIT,
            output_audio_format=self._AudioFormat.PCM_24000HZ_MONO_16BIT,
            enable_input_audio_transcription=True,
            input_audio_transcription_model='gummy-realtime-v1',
            enable_turn_detection=True,
            turn_detection_type='server_vad',
            turn_detection_threshold=0.5,
            instructions=self.session.config['instructions'],
        )
        self.session.conversation = conversation

    def append_audio(self, audio_b64: str):
        if self.session.conversation:
            self.session.conversation.append_audio(audio_b64)

    def append_video(self, video_b64: str):
        if self.session.conversation:
            self.session.conversation.append_video(video_b64)

    def preprocess_video_b64(self, video_b64: str) -> Optional[str]:
        """DashScope ws 单帧上限 262144B。
        超阈值就 Pillow 重编码 JPEG；若一次缩放后仍超阈值，自动把长边
        再砍半（max_edge → /2 → /2），最多三次。仍超就丢弃。
        """
        if not Settings.DASHSCOPE_VIDEO_COMPRESS:
            return video_b64
        max_b64 = Settings.DASHSCOPE_VIDEO_MAX_B64
        if len(video_b64) <= max_b64:
            return video_b64

        try:
            from PIL import Image
        except ImportError:
            logger.warning(
                f"[{self.session.session_id}] 视频帧超 {max_b64}B 但未安装 Pillow，丢弃"
            )
            return None

        original_len = len(video_b64)
        try:
            raw = base64.b64decode(video_b64)
            img = Image.open(io.BytesIO(raw))
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
        except Exception as e:
            logger.warning(
                f"[{self.session.session_id}] 视频帧解码失败({type(e).__name__}: {e})，丢弃"
            )
            return None

        max_edge = Settings.DASHSCOPE_VIDEO_MAX_LONG_EDGE
        quality = Settings.DASHSCOPE_VIDEO_JPEG_Q
        new_b64 = None
        used_edge = max_edge
        for attempt in range(3):
            target_edge = max(160, max_edge >> attempt)  # 640 → 320 → 160
            try:
                pic = img.copy()
                if max(pic.size) > target_edge:
                    pic.thumbnail((target_edge, target_edge), Image.LANCZOS)
                buf = io.BytesIO()
                pic.save(buf, format='JPEG', quality=quality,
                         optimize=False, progressive=False)
                candidate = base64.b64encode(buf.getvalue()).decode('ascii')
            except Exception as e:
                logger.warning(
                    f"[{self.session.session_id}] 视频帧重编码失败"
                    f"({type(e).__name__}: {e})，丢弃"
                )
                return None
            used_edge = target_edge
            if len(candidate) <= max_b64:
                new_b64 = candidate
                break
            logger.debug(
                f"[{self.session.session_id}] 视频帧 {original_len}B 缩到长边 {target_edge}px "
                f"仍 {len(candidate)}B > {max_b64}B，继续回退"
            )

        if new_b64 is None:
            logger.warning(
                f"[{self.session.session_id}] 视频帧三次回退仍超阈值 "
                f"({original_len}B，最后尝试长边 {used_edge}px)，丢弃"
            )
            return None

        if not getattr(self, '_video_compress_logged', False):
            logger.info(
                f"[{self.session.session_id}] 视频帧压缩生效: "
                f"{original_len}B -> {len(new_b64)}B (q={quality}, edge={used_edge})"
            )
            self._video_compress_logged = True
        else:
            logger.debug(
                f"[{self.session.session_id}] 视频帧压缩 {original_len}B -> {len(new_b64)}B "
                f"(edge={used_edge})"
            )
        return new_b64

    def close(self):
        if self.session.conversation:
            try:
                self.session.conversation.close()
            except Exception as e:
                logger.error(f"[{self.session.session_id}] DashScope 关闭异常: {e}")

    def is_alive(self) -> bool:
        conv = self.session.conversation
        if not conv or not hasattr(conv, 'ws'):
            return False
        ws = conv.ws
        if hasattr(ws, 'sock') and ws.sock is not None:
            connected = getattr(ws.sock, 'connected', None)
            if connected is False:
                return False
        return True


# ============== Gemini ==============
class GeminiProvider(BaseProvider):
    """
    用 google-genai 的 Live API 接入 Gemini。在独立后台线程内跑事件循环，
    HTTP/WS 主路径仍为同步调用。
    """
    name = "gemini"

    def __init__(self, session: Session):
        super().__init__(session)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._live_session = None
        self._live_ctx = None
        self._client = None
        self._closed = False
        # session_resumption handle：服务端 1000 关连接后，重连时带上即可续接上下文
        self._resume_handle: Optional[str] = None

    # ---- 工具描述 -> Gemini tools ----
    @staticmethod
    def _convert_tools(tool_descriptions: Optional[List[dict]]):
        if not tool_descriptions:
            return None
        decls = []
        for t in tool_descriptions:
            decls.append({
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            })
        return [{"function_declarations": decls}]

    def connect(self):
        try:
            from google import genai
        except Exception as e:
            raise RuntimeError(
                "未安装 google-genai，请先 pip install google-genai"
            ) from e

        from .gemini_callback import GeminiCallback
        callback = GeminiCallback(self.session)
        self.session.callback = callback

        # Live API 必须走 v1beta；非 Live 模型走 v1beta 也兼容
        self._client = genai.Client(
            api_key=Settings.GEMINI_API_KEY,
            http_options={"api_version": "v1beta"},
        )

        # 工具调用统一由 instructions 引导模型输出 JSON，不使用厂商原生 tools 参数
        tools = None  # self._convert_tools(self.session.config.get('tool_descriptions'))
        config = {
            # Live preview 模型只接受 AUDIO 这一种 modality，选 TEXT 服务端会
            # 直接 1007 拒连。我们要的"只要文字"通过 output_audio_transcription
            # 拿模型音频的文字稿，callback 端把 audio_delta 丢弃即可。
            "response_modalities": ["AUDIO"],
            "system_instruction": self.session.config['instructions'],
            "speech_config": {
                "voice_config": {
                    "prebuilt_voice_config": {"voice_name": Settings.GEMINI_VOICE}
                }
            },
            # input_audio_transcription：用户语音 → 文字（日志里"🗣️ 识别"）
            # output_audio_transcription：AI 音频 → 文字（推给前端代替 audio_delta）
            # language_codes 在 Developer API 模式下不可用（仅 Enterprise/Vertex 支持），
            # 中英漂移要靠 system_instruction 第一句钉死语种。
            "input_audio_transcription": {},
            "output_audio_transcription": {},
            # 视频帧分辨率档位（low / medium / high），medium 平衡
            "media_resolution": "MEDIA_RESOLUTION_MEDIUM",
            # 长会话保护：超过 trigger_tokens 时把上下文滑窗压缩到 target_tokens
            "context_window_compression": {
                "trigger_tokens": 104857,
                "sliding_window": {"target_tokens": 52428},
            },
            # 启用 session resumption：服务端会下发 new_handle，断连后重连带上 handle
            # 即可续上语境，避免一轮回复后被 1000 OK 关连接 → 重连后丢上下文。
            "session_resumption": (
                {"handle": self._resume_handle} if self._resume_handle else {}
            ),
        }
        if tools:
            config["tools"] = tools

        ready = threading.Event()
        error_holder = {"err": None}

        def _runner():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._async_main(config, ready, error_holder))
            except Exception as e:
                error_holder["err"] = e
                ready.set()

        self._thread = threading.Thread(target=_runner, daemon=True)
        self._thread.start()
        ready.wait(timeout=15)
        if error_holder["err"]:
            raise error_holder["err"]

    async def _async_main(self, config, ready: threading.Event, error_holder: dict):
        try:
            self._live_ctx = self._client.aio.live.connect(
                model=Settings.GEMINI_MODEL_NAME,
                config=config,
            )
            self._live_session = await self._live_ctx.__aenter__()
            logger.info(f"[{self.session.session_id}] Gemini Live 已连接")
            ready.set()
            await self._receive_loop()
        except Exception as e:
            logger.error(f"[{self.session.session_id}] Gemini Live 连接异常: {e}")
            error_holder["err"] = e
            ready.set()
        finally:
            try:
                if self._live_ctx is not None:
                    await self._live_ctx.__aexit__(None, None, None)
            except Exception:
                pass
            self._live_session = None
            self._live_ctx = None

    async def _receive_loop(self):
        callback = self.session.callback
        try:
            async for message in self._live_session.receive():
                if self._closed:
                    break
                # 抓 session_resumption_update：服务端会周期性下发 new_handle，
                # 重连时带上 handle 即可续接上下文（包含已识别的对话历史）。
                sru = getattr(message, 'session_resumption_update', None)
                if sru is not None:
                    new_handle = getattr(sru, 'new_handle', None)
                    resumable = getattr(sru, 'resumable', None)
                    if new_handle and resumable is not False:
                        self._resume_handle = new_handle
                # 服务端通知"连接将被关闭"（GoAway）：提前置 reconnect_needed，
                # 让上层在下一次 tick 把 provider 重建掉，避免再灌音频撞 1000 OK。
                if getattr(message, 'go_away', None) is not None:
                    logger.info(
                        f"[{self.session.session_id}] Gemini 收到 GoAway，准备重连"
                    )
                    self.session.reconnect_needed = True
                try:
                    callback.on_message(message)
                except Exception as e:
                    logger.error(f"[{self.session.session_id}] Gemini on_message 异常: {e}")
        except Exception as e:
            if not self._closed:
                logger.error(f"[{self.session.session_id}] Gemini 接收循环异常: {e}")
                self.session.reconnect_needed = True

    # ---- 推流 ----
    def _submit(self, coro):
        if not self._loop or self._closed:
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        except Exception as e:
            logger.error(f"[{self.session.session_id}] Gemini 任务调度失败: {e}")

    def append_audio(self, audio_b64: str):
        # 已经触发重连流程时短路：上层会重建 provider，期间继续 send 只会撞
        # "sent 1000 (OK); then received 1000 (OK)" 刷错日志
        if not self._live_session or self._closed or self.session.reconnect_needed:
            return
        try:
            audio_bytes = base64.b64decode(audio_b64)
        except Exception as e:
            logger.error(f"[{self.session.session_id}] Gemini 音频解码失败: {e}")
            return

        async def _send():
            try:
                await self._live_session.send_realtime_input(
                    audio={"data": audio_bytes, "mime_type": "audio/pcm;rate=16000"}
                )
            except Exception as e:
                if not self.session.reconnect_needed:
                    logger.error(f"[{self.session.session_id}] Gemini 发送音频异常: {e}")
                self.session.reconnect_needed = True
        self._submit(_send())

    def append_video(self, video_b64: str):
        if not self._live_session or self._closed or self.session.reconnect_needed:
            return
        try:
            img_bytes = base64.b64decode(video_b64)
        except Exception as e:
            logger.error(f"[{self.session.session_id}] Gemini 视频解码失败: {e}")
            return

        async def _send():
            try:
                await self._live_session.send_realtime_input(
                    video={"data": img_bytes, "mime_type": "image/jpeg"}
                )
            except Exception as e:
                if not self.session.reconnect_needed:
                    logger.error(f"[{self.session.session_id}] Gemini 发送视频异常: {e}")
                self.session.reconnect_needed = True
        self._submit(_send())

    def close(self):
        self._closed = True
        if self._loop and self._live_session:
            async def _bye():
                try:
                    await self._live_ctx.__aexit__(None, None, None)
                except Exception:
                    pass
            try:
                asyncio.run_coroutine_threadsafe(_bye(), self._loop)
            except Exception:
                pass
        if self._loop:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass

    def is_alive(self) -> bool:
        return bool(self._live_session) and not self._closed


class GeminiBatchProvider(BaseProvider):
    """
    非 Live 版 Gemini provider。
    直接把客户端 PCM 包成 WAV，通过 OpenAI 兼容端点的 input_audio content part
    发给模型。本地用 RMS 静音看门狗判断句末，避免 ASR。
    """
    name = "gemini_batch"

    def __init__(self, session: Session):
        super().__init__(session)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._closed = False
        self._latest_image: Optional[bytes] = None
        self._history: list = []
        self._tools_cfg = None
        self._system_instruction = None
        self._inference_running = False

        # 音频缓冲 + 静音检测
        self._pcm_buf = bytearray()
        self._buf_lock = threading.Lock()
        self._last_voiced_ts: float = 0.0
        self._has_speech: bool = False
        self._silence_sec = float(getattr(Settings, 'GEMINI_SILENCE_SEC', 0.8))
        self._min_audio_bytes = int(getattr(Settings, 'GEMINI_MIN_AUDIO_BYTES', 16000))
        self._rms_threshold = int(getattr(Settings, 'GEMINI_RMS_THRESHOLD', 800))

    @staticmethod
    def _convert_tools_openai(tool_descriptions: Optional[List[dict]]):
        if not tool_descriptions:
            return None
        out = []
        for t in tool_descriptions:
            out.append({
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            })
        return out

    @staticmethod
    def _pcm_to_wav_bytes(pcm: bytes, sample_rate: int = 16000) -> bytes:
        num_channels = 1
        bits = 16
        byte_rate = sample_rate * num_channels * bits // 8
        block_align = num_channels * bits // 8
        data_size = len(pcm)
        return (
            b'RIFF' + struct.pack('<I', 36 + data_size) + b'WAVE'
            + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, num_channels,
                                    sample_rate, byte_rate, block_align, bits)
            + b'data' + struct.pack('<I', data_size) + pcm
        )

    def connect(self):
        try:
            from openai import OpenAI
        except Exception as e:
            raise RuntimeError("未安装 openai SDK，请先 pip install openai") from e

        from .gemini_batch_callback import GeminiBatchCallback

        self._client = OpenAI(
            api_key=Settings.GEMINI_API_KEY,
            base_url="https://www.micuapi.ai/v1",
            # base_url="https://api-slb.micuapi.ai/v1",
        )
        self.session.callback = GeminiBatchCallback(self.session)
        # 工具调用统一由 instructions 引导模型输出 JSON，不使用厂商原生 tools 参数
        self._tools_cfg = None  # self._convert_tools_openai(self.session.config.get('tool_descriptions'))
        self._system_instruction = self.session.config['instructions']

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, daemon=True
        )
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._silence_watchdog(), self._loop)

        logger.info(
            f"[{self.session.session_id}] GeminiBatch provider 已就绪 "
            f"(model={Settings.GEMINI_MODEL_NAME}, "
            f"silence={self._silence_sec}s, min_audio={self._min_audio_bytes}B, "
            f"rms_threshold={self._rms_threshold})"
        )

    def append_audio(self, audio_b64: str):
        try:
            pcm = base64.b64decode(audio_b64)
        except Exception as e:
            logger.error(f"[{self.session.session_id}] GeminiBatch 音频解码失败: {e}")
            return
        try:
            rms = audioop.rms(pcm, 2)
        except audioop.error:
            rms = 0
        now = time.time()
        with self._buf_lock:
            self._pcm_buf.extend(pcm)
            if rms >= self._rms_threshold:
                self._last_voiced_ts = now
                self._has_speech = True

    def append_video(self, video_b64: str):
        try:
            self._latest_image = base64.b64decode(video_b64)
        except Exception as e:
            logger.error(f"[{self.session.session_id}] GeminiBatch 视频解码失败: {e}")

    def commit(self, reason: str = "client"):
        if self._inference_running:
            logger.debug(f"[{self.session.session_id}] commit({reason}) 跳过：上一轮推理中")
            return
        with self._buf_lock:
            if not self._pcm_buf:
                return
            pcm = bytes(self._pcm_buf)
            self._pcm_buf.clear()
            self._has_speech = False
            self._last_voiced_ts = 0.0
        image = self._latest_image
        logger.info(
            f"[{self.session.session_id}] 🎯 commit({reason}) pcm={len(pcm)}B image={'yes' if image else 'no'}"
        )
        asyncio.run_coroutine_threadsafe(self._run_turn(pcm, image), self._loop)

    async def _silence_watchdog(self):
        """每 0.2s 检查：曾检测到有声 + buffer 足够长 + 最近 silence_sec 没有有声帧 → 自动 commit。"""
        try:
            while not self._closed:
                await asyncio.sleep(0.2)
                if self._inference_running:
                    continue
                with self._buf_lock:
                    buf_len = len(self._pcm_buf)
                    last_voiced = self._last_voiced_ts
                    has_speech = self._has_speech
                if not has_speech or buf_len < self._min_audio_bytes:
                    continue
                if time.time() - last_voiced >= self._silence_sec:
                    self.commit(reason="silence")
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[{self.session.session_id}] silence_watchdog 异常: {e}")

    async def _run_turn(self, pcm_bytes: bytes, image_bytes: Optional[bytes]):
        self._inference_running = True
        try:
            wav = self._pcm_to_wav_bytes(pcm_bytes, sample_rate=16000)
            wav_b64 = base64.b64encode(wav).decode('ascii')

            user_parts: list = []
            if image_bytes:
                img_b64 = base64.b64encode(image_bytes).decode('ascii')
                user_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                })
            user_parts.append({
                "type": "input_audio",
                "input_audio": {"data": wav_b64, "format": "wav"},
            })
            user_msg = {"role": "user", "content": user_parts}

            messages: list = []
            if self._system_instruction:
                messages.append({"role": "system", "content": self._system_instruction})
            messages.extend(self._history)
            messages.append(user_msg)

            kwargs = {
                "model": Settings.GEMINI_MODEL_NAME,
                "messages": messages,
                "stream": True,
            }
            if self._tools_cfg:
                kwargs["tools"] = self._tools_cfg

            cb = self.session.callback
            cb.on_turn_start()

            sid = self.session.session_id
            t0 = time.time()
            logger.info(
                f"[{sid}] → 调用 {Settings.GEMINI_MODEL_NAME} "
                f"audio_wav={len(wav)}B img={'yes' if image_bytes else 'no'} "
                f"history={len(self._history)} tools={'yes' if self._tools_cfg else 'no'}"
            )

            def _run_sync_stream() -> str:
                acc = ""
                n_chunks = 0
                # 60s 超时：网关挂起或上游不响应时不会一直阻塞推理槽
                stream = self._client.with_options(timeout=60.0).chat.completions.create(**kwargs)
                for chunk in stream:
                    n_chunks += 1
                    acc += cb.on_chunk(chunk)
                logger.info(f"[{sid}] ← 模型返回 {n_chunks} chunks, text_len={len(acc)}")
                return acc

            try:
                full_text = await asyncio.to_thread(_run_sync_stream)
            except Exception as e:
                dt = time.time() - t0
                logger.error(
                    f"[{sid}] ✗ 调用失败 dt={dt:.1f}s "
                    f"err_type={type(e).__name__} msg={str(e)[:500]}"
                )
                cb.on_error(e)
                return
            dt = time.time() - t0
            logger.info(f"[{sid}] ✓ 调用完成 dt={dt:.1f}s text={(full_text or '')[:80]!r}")
            cb.on_turn_complete()

            # history 不保留音频，避免 token 爆炸；只记一条占位的 user 消息和 assistant 文本
            self._history.append({"role": "user", "content": "[音频片段]"})
            if full_text:
                self._history.append({"role": "assistant", "content": full_text})
        finally:
            self._inference_running = False

    def close(self):
        self._closed = True
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def is_alive(self) -> bool:
        return not self._closed


class _GummyAsrClient:
    """
    DashScope gummy-realtime-v1 流式 ASR 包装。
    服务端 VAD 切句：每收到 sentence_end 就回调 on_sentence(text)。
    """

    def __init__(self, session_id: str, on_sentence, sample_rate: int = 16000):
        self.session_id = session_id
        self.on_sentence = on_sentence
        self.sample_rate = int(sample_rate)
        self._recognizer = None
        self._partial_text = ""
        self._lock = threading.Lock()

    def start(self):
        import dashscope
        from dashscope.audio.asr import (
            TranslationRecognizerRealtime,
            TranslationRecognizerCallback,
        )
        dashscope.api_key = Settings.DASHSCOPE_API_KEY

        outer = self
        # 每次 start() 起一个新的连接代号，让 on_open / on_close 互相对齐
        # （dashscope SDK on_close 是异步的，重连后旧连接的 on_close 经常延迟到达，
        #  没有这个序号会被误读成"刚重连完又关闭了"）
        outer._gen = getattr(outer, '_gen', 0) + 1
        gen = outer._gen

        class _Cb(TranslationRecognizerCallback):
            def on_open(self):
                logger.info(f"[{outer.session_id}] gummy ASR#{gen} 已连接")

            def on_close(self):
                logger.info(f"[{outer.session_id}] gummy ASR#{gen} 已关闭")

            def on_error(self, message):
                logger.error(f"[{outer.session_id}] gummy ASR#{gen} 错误: {message}")

            def on_event(self, request_id, transcription_result, translation_result, usage):
                tr = transcription_result
                if not tr:
                    return
                text = getattr(tr, 'text', '') or ''
                is_end = bool(getattr(tr, 'sentence_end', False))
                # 诊断：每个 gummy 事件都打一行；如果完全不出说明 feed 有问题
                if not getattr(outer, '_debug_event_counter', 0):
                    outer._debug_event_counter = 0
                outer._debug_event_counter += 1
                if outer._debug_event_counter <= 30 or is_end:
                    logger.info(
                        f"[{outer.session_id}] gummy event #{outer._debug_event_counter}"
                        f" text={text!r} is_end={is_end}"
                    )
                with outer._lock:
                    outer._partial_text = text
                    if is_end and text.strip():
                        sentence = text
                        outer._partial_text = ""
                    else:
                        sentence = None
                if sentence:
                    try:
                        outer.on_sentence(sentence, source="vad")
                    except Exception as e:
                        logger.error(f"[{outer.session_id}] on_sentence 回调异常: {e}")

        self._recognizer = TranslationRecognizerRealtime(
            model='gummy-realtime-v1',
            format='pcm',
            sample_rate=self.sample_rate,
            transcription_enabled=True,
            translation_enabled=False,
            callback=_Cb(),
        )
        self._recognizer.start()

    def feed(self, pcm_bytes: bytes):
        if not self._recognizer:
            # 之前因长时间静默被服务端关掉，重新拉起一个
            try:
                self.start()
                logger.info(f"[{self.session_id}] gummy ASR 重连成功")
            except Exception as e:
                logger.error(f"[{self.session_id}] gummy ASR 重连失败: {e}")
                self._recognizer = None
                return
        try:
            self._recognizer.send_audio_frame(pcm_bytes)
            # 诊断：累计 feed 的字节数，每 ~3 秒打一行；和 gummy event 对照
            cnt = getattr(self, '_feed_total', 0) + len(pcm_bytes)
            self._feed_total = cnt
            last = getattr(self, '_feed_last_log', 0)
            if cnt - last >= 96000:  # ~3s @ 16k/16bit
                logger.info(
                    f"[{self.session_id}] gummy feed 累计 {cnt}B "
                    f"(~{cnt/32000:.1f}s @ 16k/16bit/mono)"
                )
                self._feed_last_log = cnt
        except Exception as e:
            msg = str(e)
            # gummy 服务端在长时间静默后主动断开（"has stopped"），
            # 把 recognizer 标记为失效，下一次 feed 时会自动重建
            if 'stopped' in msg.lower() or 'closed' in msg.lower():
                if not getattr(self, '_stopped_logged', False):
                    logger.warning(
                        f"[{self.session_id}] gummy ASR 已被服务端关闭，下一次 feed 会自动重连: {msg}"
                    )
                    self._stopped_logged = True
                try:
                    self._recognizer.stop()
                except Exception:
                    pass
                self._recognizer = None
                self._stopped_logged = False  # 重连后允许再警告一次
            else:
                logger.error(f"[{self.session_id}] gummy ASR feed 异常: {e}")

    def flush_partial(self) -> str:
        with self._lock:
            text = self._partial_text
            self._partial_text = ""
        return text

    def stop(self):
        if self._recognizer:
            try:
                self._recognizer.stop()
            except Exception:
                pass
            self._recognizer = None


class GLMRealtimeProvider(BaseProvider):
    """
    智谱 GLM-Realtime WebSocket 接入。
    端点: wss://open.bigmodel.cn/api/paas/v4/realtime
    鉴权: Authorization: Bearer {ZHIPU_API_KEY}（服务端鉴权方式）
    输入音频: PCM 16kHz/16bit/mono → 包成 WAV 容器，base64 后随 input_audio_buffer.append 发送
    输入视频: JPEG base64 → input_audio_buffer.append_video_frame
    server_vad 模式：服务端自动检测说话结束并触发推理，无需 commit
    """
    name = "glm"

    def __init__(self, session: Session):
        super().__init__(session)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ws = None
        self._closed = False
        self._send_q: Optional[asyncio.Queue] = None
        self._tools_cfg = self._convert_tools(None)
        self._pcm_buf = bytearray()
        self._pcm_lock = threading.Lock()
        self._flush_interval_ms = int(getattr(Settings, 'GLM_AUDIO_FLUSH_MS', 80))
        self._flusher_task = None
        # video_passive 模式本地 VAD：服务端不发 speech_stopped，
        # 客户端按 RMS 能量做静音判停，连续 silence_ms 没声音 → commit + response.create
        self._last_voice_ts = 0.0   # 最近一次"有人声"的时间（s）
        self._has_voiced = False    # 本轮是否听到过有效语音
        self._response_pending = False  # 上一次 commit 后还没收到 response.done
        self._response_lock = threading.Lock()

    @staticmethod
    def _convert_tools(tool_descriptions: Optional[List[dict]]):
        if not tool_descriptions:
            return None
        out = []
        for t in tool_descriptions:
            out.append({
                "type": "function",
                "name": t.get("name"),
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {"type": "object", "properties": {}}),
            })
        return out

    @staticmethod
    def _pcm_to_wav_bytes(pcm: bytes, sample_rate: int = 16000) -> bytes:
        import struct
        num_channels = 1
        bits_per_sample = 16
        byte_rate = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8
        data_size = len(pcm)
        return (
            b'RIFF' + struct.pack('<I', 36 + data_size) + b'WAVE'
            + b'fmt ' + struct.pack('<IHHIIHH', 16, 1, num_channels, sample_rate,
                                    byte_rate, block_align, bits_per_sample)
            + b'data' + struct.pack('<I', data_size) + pcm
        )

    def connect(self):
        try:
            import websockets  # noqa: F401
        except Exception as e:
            raise RuntimeError("未安装 websockets，请先 pip install websockets") from e

        from .glm_realtime_callback import GLMRealtimeCallback
        self.session.callback = GLMRealtimeCallback(self.session)
        # 工具调用统一由 instructions 引导模型输出 JSON，不使用厂商原生 tools 参数
        self._tools_cfg = None  # self._convert_tools(self.session.config.get('tool_descriptions'))

        if not Settings.ZHIPU_API_KEY:
            raise RuntimeError("ZHIPU_API_KEY 未配置，无法连接 GLM-Realtime")

        ready = threading.Event()
        error_holder = {"err": None}

        def _runner():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.run_until_complete(self._async_main(ready, error_holder))
            except Exception as e:
                error_holder["err"] = e
                ready.set()

        self._thread = threading.Thread(target=_runner, daemon=True)
        self._thread.start()
        ready.wait(timeout=15)
        if error_holder["err"]:
            raise error_holder["err"]

    async def _async_main(self, ready: threading.Event, error_holder: dict):
        import websockets
        url = Settings.GLM_REALTIME_URL
        headers = [("Authorization", f"Bearer {Settings.ZHIPU_API_KEY}")]
        try:
            try:
                self._ws = await websockets.connect(url, additional_headers=headers)
            except TypeError:
                self._ws = await websockets.connect(url, extra_headers=headers)
            self._send_q = asyncio.Queue()
            # 创建"session.updated 已到达"事件，让客户端在它真正切到 video_passive
            # 之前不要往 ws 里发音视频包；否则首批包会按服务端默认的 chat_mode=audio
            # 进 VAD/推理上下文，切到 video_passive 后 VAD 追不上。
            self._session_ready = asyncio.Event()
            await self._send_session_update()
            logger.info(
                f"[{self.session.session_id}] GLM-Realtime 已连接 "
                f"(model={Settings.GLM_MODEL_NAME}, chat_mode={Settings.GLM_CHAT_MODE})"
            )
            ready.set()  # 上层可以开始往 _pcm_buf 灌数据，但实际发送会等 _session_ready
            sender = asyncio.create_task(self._sender_loop())
            flusher = asyncio.create_task(self._audio_flush_loop())
            self._flusher_task = flusher
            try:
                await self._receive_loop()
            finally:
                sender.cancel()
                flusher.cancel()
        except Exception as e:
            logger.error(f"[{self.session.session_id}] GLM 连接异常: {e}")
            error_holder["err"] = e
            ready.set()
        finally:
            try:
                if self._ws is not None:
                    await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def _send_session_update(self):
        # video_passive 模式必须用 tts_source=zhipu/huoshan 走"模型生文字 → TTS 合音频"链路。
        # 默认/e2e 模式下模型直接输出 audio token，没有"先生成文字"步骤，response.text.delta 永远空。
        # 我们其实不需要 GLM 的音频（下游有本地 TTS），但 modalities 仍需保留 audio 让模型走文字链路；
        # callback 只消费 text/audio_transcript 事件，audio.delta 直接忽略。
        beta = {"chat_mode": Settings.GLM_CHAT_MODE, "tts_source": "zhipu", "auto_search": False}
        # 与 dashscope 链路一致：用 ws 建连请求里解析出来的 instructions（已并入工具列表 + 护栏），
        # 不再被 GLM_DEBUG_MINIMAL_SESSION 强制覆盖成最小排障串。
        instructions = self.session.config.get('instructions', '')
        session_cfg = {
            "modalities": ["audio", "text"],
            "input_audio_format": "wav",
            "output_audio_format": "pcm",
            "instructions": instructions,
            # session.updated 默认 input_audio_transcription.enabled=false，
            # 用户语音不会被转写成文字喂给 LLM，video_passive 下 LLM 拿不到 query 就不回。
            "input_audio_transcription": {"model": "standard", "enabled": True},
            # 默认 threshold=0.5 对很多麦克风（USB 摄像头/带降噪耳机）能量都跨不过去，
            # 服务端 server_vad 永远不触发 speech_started → 永远不会回复。
            # 0.3 在嘈杂环境下可能误触发，安静环境下足够灵敏；如仍无响应可再降到 0.2。
            # create_response=False：让服务端只做 ASR 不自动建 response，
            # 由客户端本地 VAD 唯一控制 commit + response.create，避免双触发抢占。
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.3,
                "silence_duration_ms": 300,
                "prefix_padding_ms": 300,
                "create_response": False,
            },
            "beta_fields": beta,
        }

        if self._tools_cfg:
            session_cfg["tools"] = self._tools_cfg
        evt = {
            "event_id": f"evt_{int(time.time()*1000)}",
            "type": "session.update",
            "client_timestamp": int(time.time() * 1000),
            "session": session_cfg,
        }
        logger.info(
            f"[{self.session.session_id}] GLM session.update payload: "
            f"{json.dumps(session_cfg, ensure_ascii=False)}"
        )
        await self._ws.send(json.dumps(evt, ensure_ascii=False))

    async def _sender_loop(self):
        try:
            # 等 session.updated 落地，避免首批音视频包按服务端默认 chat_mode=audio
            # 进推理上下文导致 video_passive 模式不响应
            await self._session_ready.wait()
            while not self._closed and self._ws is not None:
                msg = await self._send_q.get()
                if msg is None:
                    break
                try:
                    await self._ws.send(msg)
                except Exception as e:
                    logger.error(f"[{self.session.session_id}] GLM send 异常: {e}")
                    break
        except asyncio.CancelledError:
            return

    async def _receive_loop(self):
        cb = self.session.callback
        try:
            async for raw in self._ws:
                if self._closed:
                    break
                # 拦截两类事件：
                # 1) session.updated 解开 _session_ready 阀门
                # 2) input_audio_buffer.speech_stopped（仅 video 模式）
                #    video_passive / video_proactive 下服务端 server_vad 不会自动 commit + 触发回复，
                #    必须客户端在说话结束时主动发 commit + response.create（与官方
                #    low_level_sample_video.py 的事件序列一致）
                try:
                    evt = json.loads(raw) if isinstance(raw, str) else raw
                    etype = evt.get("type")
                    if etype == "session.updated" and not self._session_ready.is_set():
                        self._session_ready.set()
                        logger.info(
                            f"[{self.session.session_id}] GLM session.updated 已确认，"
                            f"开始发送音视频"
                        )
                    elif etype == "response.done":
                        # 上一轮回复结束，允许本地 VAD 触发下一轮
                        with self._response_lock:
                            self._response_pending = False
                except Exception:
                    pass
                try:
                    cb.on_message(raw)
                except Exception as e:
                    logger.error(f"[{self.session.session_id}] GLM on_message 异常: {e}")
        except Exception as e:
            if not self._closed:
                logger.error(f"[{self.session.session_id}] GLM 接收循环异常: {e}")
                self.session.reconnect_needed = True

    def _enqueue(self, payload: dict):
        if not self._loop or self._closed or self._send_q is None:
            return
        text = json.dumps(payload, ensure_ascii=False)
        try:
            asyncio.run_coroutine_threadsafe(self._send_q.put(text), self._loop)
        except Exception as e:
            logger.error(f"[{self.session.session_id}] GLM 入队失败: {e}")

    def append_audio(self, audio_b64: str):
        try:
            pcm = base64.b64decode(audio_b64)
        except Exception as e:
            logger.error(f"[{self.session.session_id}] GLM 音频解码失败: {e}")
            return
        with self._pcm_lock:
            # 调试用：把前 10 秒 PCM dump 成文件，用 ffplay 验证编码格式。
            # 平时关掉，需要排查 GLM 收音问题时把 GLM_DUMP_AUDIO 设成 True。
            GLM_DUMP_AUDIO = False
            if GLM_DUMP_AUDIO and not self._pcm_buf and not getattr(self, '_audio_magic_logged', False):
                magic = pcm[:8].hex() if pcm else ''
                logger.info(
                    f"[{self.session.session_id}] GLM 首包音频 len={len(pcm)} magic={magic} "
                    f"(裸PCM应无固定magic；'52494646'=RIFF/WAV)"
                )
                self._audio_magic_logged = True
                self._dump_total = 0
                self._dump_path = f"glm_audio_dump_{self.session.session_id}_16k_mono_s16le.pcm"
                try:
                    self._dump_fp = open(self._dump_path, "wb")
                    logger.info(
                        f"[{self.session.session_id}] 开始 dump 前 10 秒 PCM → {self._dump_path}"
                        f"（用 ffplay -f s16le -ar 16000 -ac 1 {self._dump_path} 验证）"
                    )
                except Exception as e:
                    logger.warning(f"[{self.session.session_id}] dump 文件打开失败: {e}")
                    self._dump_fp = None
            # 写 dump（最多 10 秒 = 320000 字节 @ 16kHz/16bit/mono）
            fp = getattr(self, '_dump_fp', None)
            if GLM_DUMP_AUDIO and fp is not None:
                fp.write(pcm)
                self._dump_total += len(pcm)
                if self._dump_total >= 320000:
                    try:
                        fp.close()
                    except Exception:
                        pass
                    logger.info(
                        f"[{self.session.session_id}] dump 完成 ({self._dump_total}B) → {self._dump_path}"
                    )
                    self._dump_fp = None
            self._pcm_buf.extend(pcm)

    async def _audio_flush_loop(self):
        """
        GLM 服务端 VAD 期望"等长 100ms WAV 包"才能稳定累计检测窗。
        每 100ms 触发一次，从 buffer 切固定 3200B (=100ms@16k/16bit/mono) 一片，
        多余的留到下次；不够 3200B 就什么也不发，等下个 tick。
        实际发送由 _sender_loop 在 session.updated 后才放行。

        video_passive 模式下额外做本地 VAD：服务端 server_vad 在该模式下不稳定（实测
        threshold 0.3 也常常不触发），所以客户端按 RMS 能量做兜底；
        连续静音 silence_ms 后主动 commit + response.create。
        本地 VAD 是唯一的提交路径，避免和服务端兜底再叠加引发双触发。
        """
        import struct as _struct
        CHUNK_BYTES = 3200  # 100ms @ 16kHz / 16bit / mono
        sent_packets = 0
        chat_mode = (Settings.GLM_CHAT_MODE or '').lower()
        local_vad = chat_mode.startswith('video')
        rms_threshold = int(getattr(Settings, 'GEMINI_RMS_THRESHOLD', 800))
        silence_ms = 700      # 连续 N ms 静音 → 触发停顿
        min_voice_ms = 300    # 至少要听到这么久人声才会 commit，避免误触发
        try:
            interval = 0.1
            while not self._closed:
                await asyncio.sleep(interval)
                chunks: list[bytes] = []
                with self._pcm_lock:
                    while len(self._pcm_buf) >= CHUNK_BYTES:
                        chunks.append(bytes(self._pcm_buf[:CHUNK_BYTES]))
                        del self._pcm_buf[:CHUNK_BYTES]
                for pcm in chunks:
                    try:
                        wav = self._pcm_to_wav_bytes(pcm, sample_rate=16000)
                        wav_b64 = base64.b64encode(wav).decode('ascii')
                    except Exception as e:
                        logger.error(f"[{self.session.session_id}] GLM PCM→WAV 失败: {e}")
                        continue
                    self._enqueue({
                        "type": "input_audio_buffer.append",
                        "audio": wav_b64,
                        "client_timestamp": int(time.time() * 1000),
                    })
                    sent_packets += 1
                    if sent_packets % 20 == 0:
                        logger.info(
                            f"[{self.session.session_id}] GLM 已发 {sent_packets} 包 audio "
                            f"(每包定长 pcm={len(pcm)}B wav={len(wav)}B)"
                        )
                    if local_vad:
                        try:
                            n_samples = len(pcm) // 2
                            if n_samples > 0:
                                samples = _struct.unpack(f'<{n_samples}h', pcm)
                                avg_abs = sum(abs(s) for s in samples) / n_samples
                                now = time.time()
                                if avg_abs >= rms_threshold:
                                    self._last_voice_ts = now
                                    if not self._has_voiced:
                                        self._voice_start_ts = now
                                        self._has_voiced = True
                                        logger.info(
                                            f"[{self.session.session_id}] GLM 本地 VAD: "
                                            f"speech_started (avg_abs={avg_abs:.0f})"
                                        )
                                if (self._has_voiced
                                        and (now - self._last_voice_ts) * 1000 >= silence_ms
                                        and (self._last_voice_ts - getattr(self, '_voice_start_ts', 0)) * 1000 >= min_voice_ms):
                                    with self._response_lock:
                                        already_pending = self._response_pending
                                        if not already_pending:
                                            self._response_pending = True
                                    if not already_pending:
                                        ts = int(time.time() * 1000)
                                        self._enqueue({
                                            "type": "input_audio_buffer.commit",
                                            "client_timestamp": ts,
                                        })
                                        self._enqueue({
                                            "type": "response.create",
                                            "client_timestamp": ts,
                                        })
                                        logger.info(
                                            f"[{self.session.session_id}] GLM 本地 VAD: "
                                            f"silence_{silence_ms}ms -> commit + response.create"
                                        )
                                    self._has_voiced = False
                        except Exception as e:
                            logger.error(
                                f"[{self.session.session_id}] GLM 本地 VAD 异常: {e}"
                            )
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[{self.session.session_id}] GLM audio_flush_loop 异常: {e}")

    def _normalize_video_landscape(self, video_b64: str) -> Optional[str]:
        """
        GLM video_passive 在竖屏帧（h>w）下不响应——视觉编码器对竖屏帧失效。
        把任意输入归一到不超过 GLM_VIDEO_MAX_WIDTH x GLM_VIDEO_MAX_HEIGHT 的横屏 JPEG，
        竖屏帧用 letterbox（黑边补齐）保持原始内容方向，不做旋转。
        """
        try:
            from PIL import Image
        except ImportError:
            logger.warning(
                f"[{self.session.session_id}] GLM 视频归一化需要 Pillow，未安装；原样发送"
            )
            return video_b64

        target_w = Settings.GLM_VIDEO_TARGET_W
        target_h = Settings.GLM_VIDEO_TARGET_H
        quality = Settings.GLM_VIDEO_JPEG_Q
        try:
            raw = base64.b64decode(video_b64)
            img = Image.open(io.BytesIO(raw))
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            src_w, src_h = img.size
            # 等比缩放使其能塞进 target_w x target_h
            scale = min(target_w / src_w, target_h / src_h)
            new_w = max(1, int(src_w * scale))
            new_h = max(1, int(src_h * scale))
            resized = img.resize((new_w, new_h), Image.LANCZOS)
            # letterbox 到固定 target_w x target_h 横屏画布
            canvas = Image.new('RGB', (target_w, target_h), (0, 0, 0))
            canvas.paste(resized, ((target_w - new_w) // 2, (target_h - new_h) // 2))
            buf = io.BytesIO()
            canvas.save(buf, format='JPEG', quality=quality, optimize=False, progressive=False)
            new_b64 = base64.b64encode(buf.getvalue()).decode('ascii')
        except Exception as e:
            logger.warning(
                f"[{self.session.session_id}] GLM 视频归一化失败({type(e).__name__}: {e})，原样发送"
            )
            return video_b64

        if not getattr(self, '_video_normalize_logged', False):
            logger.info(
                f"[{self.session.session_id}] GLM 视频归一化生效: "
                f"src={src_w}x{src_h} -> canvas={target_w}x{target_h} "
                f"(b64 {len(video_b64)}B -> {len(new_b64)}B, q={quality})"
            )
            self._video_normalize_logged = True
        return new_b64

    def append_video(self, video_b64: str):
        if not video_b64:
            return
        if Settings.GLM_CHAT_MODE == 'audio':
            return
        # 强制归一为横屏 JPEG。竖屏帧（h>w）会让 GLM video_passive 视觉链路失效，
        # 同时帧过大也会被网关静默丢；这里一次性解决两个问题。
        if Settings.GLM_VIDEO_NORMALIZE:
            normalized = self._normalize_video_landscape(video_b64)
            if normalized is None:
                return
            video_b64 = normalized
        # 每 N 帧抽检一次格式：JPEG 'ffd8ff' / PNG '89504e47' / H.264 NAL '00000001' or '000001'
        # GLM 只接 JPEG/PNG，H.264 NAL 会被服务端静默丢弃且可能拖累 VAD/推理
        n = getattr(self, '_video_frame_count', 0)
        self._video_frame_count = n + 1
        if n == 0 or n == 5 or n % 30 == 0:
            try:
                head = base64.b64decode(video_b64[:32])[:6].hex()
            except Exception:
                head = '?'
            if head.startswith('ffd8ff'):
                tag = 'JPEG ✓'
            elif head.startswith('89504e47'):
                tag = 'PNG ✓'
            elif head.startswith('00000001') or head.startswith('000001'):
                tag = 'H.264 NAL ✗（GLM 不支持，需要先解码再 JPEG 编码）'
            else:
                tag = f'未知格式 ✗（前 6 字节 {head}）'
            logger.info(
                f"[{self.session.session_id}] GLM 视频抽检 #{n} magic={head} -> {tag} "
                f"(b64_len={len(video_b64)})"
            )
        self._enqueue({
            "type": "input_audio_buffer.append_video_frame",
            "video_frame": video_b64,
            "client_timestamp": int(time.time() * 1000),
        })
        # self._enqueue({
        #     "type": "append_video_frame",
        #     "frame": video_b64,
        #     "client_timestamp": int(time.time() * 1000),
        # })

    def commit(self, reason: str = "client"):
        # server_vad 模式下服务端自动 commit；client_vad 模式才需要这条
        self._enqueue({
            "type": "input_audio_buffer.commit",
            "client_timestamp": int(time.time() * 1000),
        })

    def close(self):
        self._closed = True
        if self._send_q is not None and self._loop:
            try:
                asyncio.run_coroutine_threadsafe(self._send_q.put(None), self._loop)
            except Exception:
                pass
        if self._loop:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass

    def is_alive(self) -> bool:
        return (self._ws is not None) and not self._closed


# ============== Doubao RTC ==============
class DoubaoRtcProvider(BaseProvider):
    """
    豆包 RTC 接入：服务端拉起 RTC AI Agent（StartVoiceChat），同时本进程
    spawn 一个 native helper（rtc_helper.exe）作为"虚拟人类用户"加入同一个
    房间，把客户端 ws 推过来的 PCM/JPEG 通过 helper 发布到 RTC 房间里。
    Agent 在房间里订阅 helper 发布的流，做 ASR/LLM/TTS，结果通过数据通道
    binary message 回到 helper → stdout → Python → ws 客户端。
    """
    name = "doubao_rtc"

    HELPER_BIN = os.environ.get(
        'RTC_HELPER_BIN',
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                     'bin', 'rtc_helper.exe'),
    )

    # 协议帧 type
    _IN_JOIN       = 0x01
    _IN_AUDIO_PCM  = 0x02
    _IN_VIDEO_JPEG = 0x03
    _IN_LEAVE      = 0x04
    _OUT_READY      = 0xA1
    _OUT_SUBTITLE   = 0xA2
    _OUT_ERROR      = 0xA3
    _OUT_ROOM_EVENT = 0xA4
    _OUT_LOG        = 0xA5

    def __init__(self, session: Session):
        super().__init__(session)
        self._room_id: Optional[str] = None
        self._task_id: Optional[str] = None
        self._closed = False
        self._proc = None
        self._stdin_lock = threading.Lock()
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def connect(self):
        from . import doubao_rtc
        import subprocess

        sid = self.session.session_id
        room_id = f"rtc_{sid}"
        task_id = doubao_rtc.new_task_id()
        agent_user_id = doubao_rtc.derive_agent_user_id(sid)
        human_user_id = doubao_rtc.derive_human_user_id(sid)

        resp = doubao_rtc.start_voice_chat(
            room_id=room_id,
            task_id=task_id,
            instructions=self.session.config.get('instructions', ''),
            agent_user_id=agent_user_id,
            target_user_id=human_user_id,
        )
        try:
            rtc_token = doubao_rtc.generate_rtc_token(
                app_id=Settings.VOLC_APP_ID,
                app_key=Settings.VOLC_RTC_APP_KEY,
                room_id=room_id,
                user_id=human_user_id,
            )
        except Exception as e:
            logger.error(f"[{sid}] 生成 RTC token 失败: {e}")
            rtc_token = None

        self._room_id = room_id
        self._task_id = task_id
        self.session.config['doubao_rtc'] = {
            'app_id': Settings.VOLC_APP_ID,
            'room_id': room_id,
            'user_id': human_user_id,
            'agent_user_id': agent_user_id,
            'task_id': task_id,
            'token': rtc_token,
            'openapi_response': resp,
        }
        logger.info(
            f"[{sid}] DoubaoRtcProvider StartVoiceChat 完成 room={room_id} "
            f"task={task_id} agent={agent_user_id}"
        )

        if not rtc_token:
            raise RuntimeError("未生成 RTC token，无法启动 helper")

        # callback：解 helper 字幕 → 转 ws 协议（跟其他 provider 对齐）
        from .doubao_rtc_callback import DoubaoRtcCallback
        self.session.callback = DoubaoRtcCallback(self.session)

        if not os.path.isfile(self.HELPER_BIN):
            raise RuntimeError(
                f"找不到 rtc_helper.exe: {self.HELPER_BIN}。"
                "请先在 native/rtc_helper/ 用 cmake 编译"
            )

        # 拉子进程；helper 进程以 cwd=bin/ 启动，方便它找到同目录下的 dll
        helper_dir = os.path.dirname(self.HELPER_BIN)
        self._proc = subprocess.Popen(
            [self.HELPER_BIN],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=helper_dir,
            bufsize=0,
        )
        logger.info(f"[{sid}] rtc_helper 已启动 pid={self._proc.pid}")

        self._reader_thread = threading.Thread(
            target=self._read_loop, daemon=True, name=f"rtc-helper-out-{sid}")
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, daemon=True, name=f"rtc-helper-err-{sid}")
        self._stderr_thread.start()

        join_payload = json.dumps({
            'app_id': Settings.VOLC_APP_ID,
            'room_id': room_id,
            'user_id': human_user_id,
            'token': rtc_token,
        }, ensure_ascii=False).encode('utf-8')
        self._send_frame(self._IN_JOIN, join_payload)

        if not self._ready.wait(timeout=15):
            raise RuntimeError("rtc_helper 15s 内未上报 READY，进房失败")
        logger.info(f"[{sid}] rtc_helper READY，开始转发音视频")

    # ---- 协议 IO ----
    def _send_frame(self, ftype: int, payload: bytes):
        if not self._proc or self._proc.stdin is None or self._closed:
            return
        hdr = bytes([
            ftype,
            (len(payload) >> 24) & 0xff,
            (len(payload) >> 16) & 0xff,
            (len(payload) >> 8) & 0xff,
            (len(payload) >> 0) & 0xff,
        ])
        try:
            with self._stdin_lock:
                self._proc.stdin.write(hdr)
                if payload:
                    self._proc.stdin.write(payload)
                self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            if not self._closed:
                logger.error(f"[{self.session.session_id}] helper stdin 断开: {e}")
                self.session.reconnect_needed = True

    def _read_full(self, n: int) -> Optional[bytes]:
        out = bytearray()
        while len(out) < n:
            try:
                chunk = self._proc.stdout.read(n - len(out))
            except Exception:
                return None
            if not chunk:
                return None
            out.extend(chunk)
        return bytes(out)

    def _read_loop(self):
        sid = self.session.session_id
        try:
            while not self._closed:
                hdr = self._read_full(5)
                if hdr is None:
                    break
                ftype = hdr[0]
                length = (hdr[1] << 24) | (hdr[2] << 16) | (hdr[3] << 8) | hdr[4]
                body = self._read_full(length) if length else b''
                if body is None and length > 0:
                    break
                self._handle_out_frame(ftype, body or b'')
        finally:
            if not self._closed:
                logger.warning(f"[{sid}] helper stdout 关闭，标记 reconnect_needed")
                self.session.reconnect_needed = True

    def _stderr_loop(self):
        sid = self.session.session_id
        try:
            for line in iter(self._proc.stderr.readline, b''):
                if not line:
                    break
                logger.info(f"[{sid}] [helper] {line.decode('utf-8', 'replace').rstrip()}")
        except Exception:
            pass

    def _handle_out_frame(self, ftype: int, body: bytes):
        sid = self.session.session_id
        if ftype == self._OUT_READY:
            self._ready.set()
        elif ftype == self._OUT_SUBTITLE:
            text = body.decode('utf-8', 'replace')
            logger.info(f"[{sid}] helper subtitle: {text[:200]}")
            cb = self.session.callback
            if cb is not None and hasattr(cb, 'on_helper_subtitle'):
                try:
                    cb.on_helper_subtitle(text)
                except Exception as e:
                    logger.error(f"[{sid}] callback subtitle 异常: {e}")
        elif ftype == self._OUT_ERROR:
            logger.error(f"[{sid}] helper error: {body.decode('utf-8', 'replace')}")
        elif ftype == self._OUT_ROOM_EVENT:
            logger.info(f"[{sid}] helper room_event: {body.decode('utf-8', 'replace')}")
        else:
            logger.warning(f"[{sid}] helper 未知 frame type=0x{ftype:02x}")

    # ---- 推流 ----
    def append_audio(self, audio_b64: str):
        if self._closed or not self._ready.is_set():
            return
        try:
            pcm = base64.b64decode(audio_b64)
        except Exception as e:
            logger.error(f"[{self.session.session_id}] DoubaoRtc 音频解码失败: {e}")
            return
        self._send_frame(self._IN_AUDIO_PCM, pcm)

    def append_video(self, video_b64: str):
        if self._closed or not self._ready.is_set():
            return
        try:
            jpg = base64.b64decode(video_b64)
        except Exception as e:
            logger.error(f"[{self.session.session_id}] DoubaoRtc 视频解码失败: {e}")
            return
        self._send_frame(self._IN_VIDEO_JPEG, jpg)

    def close(self):
        if self._closed:
            return
        self._closed = True
        # 1) 通知 helper 主动 leave
        if self._proc and self._proc.stdin is not None:
            try:
                self._send_frame(self._IN_LEAVE, b'')
            except Exception:
                pass
        # 2) 关 RTC AI Agent
        if self._room_id and self._task_id:
            try:
                from . import doubao_rtc
                doubao_rtc.stop_voice_chat(room_id=self._room_id, task_id=self._task_id)
            except Exception as e:
                logger.warning(f"[{self.session.session_id}] StopVoiceChat 失败（忽略）: {e}")
        # 3) helper 进程兜底回收
        if self._proc:
            try:
                self._proc.wait(timeout=5)
            except Exception:
                try:
                    self._proc.terminate()
                except Exception:
                    pass

    def is_alive(self) -> bool:
        if self._closed or not self._room_id:
            return False
        return self._proc is not None and self._proc.poll() is None


# ============== Doubao Batch（非 RTC，走方舟 OpenAPI 兼容端点） ==============
class DoubaoBatchProvider(BaseProvider):
    """
    豆包 batch 模式：前端 WebSocket 推 PCM/JPEG（与 dashscope 完全一致），
    服务端用 dashscope 的 gummy-realtime ASR 实时把音频转文本，
    本地 RMS 静音看门狗判句尾（gummy server VAD 实测不发 sentence_end），
    每停顿一次就把当前 ASR 文本 + 最新一帧 JPEG 喂给方舟 chat.completion，
    流式返回的 text delta 复用 GeminiBatchCallback 推回 ws。
    """
    name = "doubao_batch"

    def __init__(self, session: Session):
        super().__init__(session)
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._client = None
        self._closed = False
        self._latest_image: Optional[bytes] = None
        self._history: list = []
        self._tools_cfg = None
        self._system_instruction = None
        self._inference_running = False
        self._asr: Optional[_GummyAsrClient] = None

        # 本地静音看门狗
        self._buf_lock = threading.Lock()
        self._last_voiced_ts: float = 0.0
        self._has_speech: bool = False
        self._audio_bytes_since_voice: int = 0
        self._silence_sec = float(getattr(Settings, 'DOUBAO_BATCH_SILENCE_SEC', 0.8))
        self._min_audio_bytes = int(getattr(Settings, 'DOUBAO_BATCH_MIN_AUDIO_BYTES', 16000))
        self._rms_threshold = int(getattr(Settings, 'DOUBAO_BATCH_RMS_THRESHOLD', 800))

    @staticmethod
    def _convert_tools_openai(tool_descriptions: Optional[List[dict]]):
        if not tool_descriptions:
            return None
        out = []
        for t in tool_descriptions:
            out.append({
                "type": "function",
                "function": {
                    "name": t.get("name"),
                    "description": t.get("description", ""),
                    "parameters": t.get("parameters", {"type": "object", "properties": {}}),
                },
            })
        return out

    def connect(self):
        try:
            from openai import OpenAI
        except Exception as e:
            raise RuntimeError("未安装 openai SDK，请先 pip install openai") from e

        if not Settings.DOUBAO_API_KEY:
            raise RuntimeError(
                "DOUBAO_API_KEY 未配置。在方舟控制台 → API Key 管理申请后，"
                "通过环境变量 DOUBAO_API_KEY 注入或写到 src/config.py。"
            )

        from .gemini_batch_callback import GeminiBatchCallback
        self._client = OpenAI(
            api_key=Settings.DOUBAO_API_KEY,
            base_url=Settings.DOUBAO_OPENAPI_BASE,
        )
        self.session.callback = GeminiBatchCallback(self.session)
        # 工具调用统一由 instructions 引导模型输出 JSON，不使用厂商原生 tools 参数
        self._tools_cfg = None  # self._convert_tools_openai(self.session.config.get('tool_descriptions'))
        self._system_instruction = self.session.config.get('instructions', '')

        # 起独立 asyncio loop 跑推理，避免阻塞 ws 线程
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()

        # gummy 只用于实时 ASR，sentence_end 实测不发 → on_sentence 一辈子不会被调
        # 真正的"一句话结束"由本地 RMS 静音看门狗判定，见 _silence_watchdog
        def _on_sentence(sentence: str, source: str = "vad"):
            return  # 不再使用 gummy 的 server VAD 触发

        self._asr = _GummyAsrClient(
            session_id=self.session.session_id,
            on_sentence=_on_sentence,
            sample_rate=Settings.RATE,
        )
        self._asr.start()

        # 启动静音看门狗
        asyncio.run_coroutine_threadsafe(self._silence_watchdog(), self._loop)

        logger.info(
            f"[{self.session.session_id}] DoubaoBatch provider 已就绪 "
            f"(model={Settings.DOUBAO_BATCH_MODEL}, base={Settings.DOUBAO_OPENAPI_BASE}, "
            f"silence={self._silence_sec}s, rms={self._rms_threshold})"
        )

    def append_audio(self, audio_b64: str):
        try:
            pcm = base64.b64decode(audio_b64)
        except Exception as e:
            logger.error(f"[{self.session.session_id}] DoubaoBatch 音频解码失败: {e}")
            return
        if self._asr:
            self._asr.feed(pcm)
        # RMS 静音看门狗
        try:
            rms = audioop.rms(pcm, 2)
        except audioop.error:
            rms = 0
        now = time.time()
        with self._buf_lock:
            self._audio_bytes_since_voice += len(pcm)
            if rms >= self._rms_threshold:
                self._last_voiced_ts = now
                self._has_speech = True

    async def _silence_watchdog(self):
        """每 0.2s 看一次：曾听到人声 + 累计音频够长 + 已静音 silence_sec → commit。
        commit 时从 gummy 当前的 partial_text 拿到识别结果，配合最新一帧调豆包。
        """
        try:
            while not self._closed:
                await asyncio.sleep(0.2)
                if self._inference_running:
                    continue
                with self._buf_lock:
                    has_speech = self._has_speech
                    last_voiced = self._last_voiced_ts
                    n_bytes = self._audio_bytes_since_voice
                if not has_speech or n_bytes < self._min_audio_bytes:
                    continue
                if time.time() - last_voiced < self._silence_sec:
                    continue
                # 触发：取 gummy 当前的 partial 文本作为这一句的识别结果
                text = self._asr.flush_partial() if self._asr else ""
                text = (text or '').strip()
                with self._buf_lock:
                    self._has_speech = False
                    self._audio_bytes_since_voice = 0
                if not text:
                    logger.info(
                        f"[{self.session.session_id}] silence_watchdog 触发但 ASR 还没出文本，跳过"
                    )
                    continue
                logger.info(
                    f"[{self.session.session_id}] DoubaoBatch silence -> ASR: {text!r}"
                )
                try:
                    self.session.log_user(text)
                except Exception:
                    pass
                # 硬关键词命中即直接发 tool_call，并跳过本轮模型推理
                if try_fire_keyword_tool(self.session, text):
                    continue
                asyncio.run_coroutine_threadsafe(
                    self._run_turn(text, self._latest_image), self._loop
                )
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.error(f"[{self.session.session_id}] silence_watchdog 异常: {e}")

    def append_video(self, video_b64: str):
        try:
            self._latest_image = base64.b64decode(video_b64)
        except Exception as e:
            logger.error(f"[{self.session.session_id}] DoubaoBatch 视频解码失败: {e}")

    def _shrink_jpeg(self, raw: bytes) -> bytes:
        """长边缩到 DOUBAO_BATCH_IMAGE_MAX_EDGE 并重新 JPEG 编码。
        Pillow 不可用 / 解码失败时透传原图，不阻塞主流程。
        """
        max_edge = Settings.DOUBAO_BATCH_IMAGE_MAX_EDGE
        quality = Settings.DOUBAO_BATCH_IMAGE_JPEG_Q
        try:
            from PIL import Image
        except ImportError:
            return raw
        try:
            img = Image.open(io.BytesIO(raw))
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            if max(img.size) > max_edge:
                img.thumbnail((max_edge, max_edge), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=quality, optimize=False, progressive=False)
            shrunk = buf.getvalue()
        except Exception as e:
            logger.warning(
                f"[{self.session.session_id}] DoubaoBatch 缩图失败({type(e).__name__}: {e})，原图发送"
            )
            return raw
        if not getattr(self, '_shrink_logged', False):
            logger.info(
                f"[{self.session.session_id}] DoubaoBatch 图片下采样: "
                f"{len(raw)}B -> {len(shrunk)}B (max_edge={max_edge}, q={quality})"
            )
            self._shrink_logged = True
        return shrunk

    async def _run_turn(self, user_text: str, image_bytes: Optional[bytes]):
        if self._inference_running:
            logger.debug(
                f"[{self.session.session_id}] DoubaoBatch 上一轮未结束，跳过本句"
            )
            return
        self._inference_running = True
        try:
            user_parts: list = []
            if image_bytes:
                # 服务端下采样：长边压到 DOUBAO_BATCH_IMAGE_MAX_EDGE，重新 JPEG 编码
                # OpenAI 兼容协议没有原生 image_detail 控制，靠这一步减少 prefill
                shrunk = self._shrink_jpeg(image_bytes)
                img_b64 = base64.b64encode(shrunk).decode('ascii')
                user_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                })
            user_parts.append({"type": "text", "text": user_text})
            user_msg = {"role": "user", "content": user_parts}

            messages: list = []
            if self._system_instruction:
                messages.append({"role": "system", "content": self._system_instruction})
            messages.extend(self._history)
            messages.append(user_msg)

            kwargs = {
                "model": Settings.DOUBAO_BATCH_MODEL,
                "messages": messages,
                "stream": True,
            }
            if self._tools_cfg:
                kwargs["tools"] = self._tools_cfg

            cb = self.session.callback
            cb.on_turn_start()

            sid = self.session.session_id
            t0 = time.time()
            logger.info(
                f"[{sid}] → 调用 doubao {Settings.DOUBAO_BATCH_MODEL} "
                f"text={user_text!r} img={'yes' if image_bytes else 'no'} "
                f"history={len(self._history)} tools={'yes' if self._tools_cfg else 'no'}"
            )

            def _run_sync_stream() -> str:
                acc = ""
                n_chunks = 0
                stream = self._client.with_options(timeout=60.0).chat.completions.create(**kwargs)
                for chunk in stream:
                    n_chunks += 1
                    acc += cb.on_chunk(chunk)
                logger.info(f"[{sid}] ← doubao 返回 {n_chunks} chunks, text_len={len(acc)}")
                return acc

            try:
                full_text = await asyncio.to_thread(_run_sync_stream)
            except Exception as e:
                dt = time.time() - t0
                logger.error(
                    f"[{sid}] ✗ doubao 调用失败 dt={dt:.1f}s "
                    f"err_type={type(e).__name__} msg={str(e)[:500]}"
                )
                cb.on_error(e)
                return
            dt = time.time() - t0
            logger.info(f"[{sid}] ✓ doubao 完成 dt={dt:.1f}s text={(full_text or '')[:80]!r}")
            cb.on_turn_complete()

            self._history.append({"role": "user", "content": user_text})
            if full_text:
                self._history.append({"role": "assistant", "content": full_text})
            # 简单的 history 上限：最多保留最近 10 轮
            if len(self._history) > 20:
                self._history = self._history[-20:]
        finally:
            self._inference_running = False

    def close(self):
        self._closed = True
        if self._asr:
            try:
                self._asr.stop()
            except Exception:
                pass
            self._asr = None
        if self._loop:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass

    def is_alive(self) -> bool:
        return not self._closed


def create_provider(session: Session, model_name: str) -> BaseProvider:
    name = (model_name or '').lower()
    if name.startswith('glm-realtime') or name.startswith('glm_realtime'):
        return GLMRealtimeProvider(session)
    if name.startswith('gemini'):
        if 'live' in name or 'native-audio-dialog' in name:
            return GeminiProvider(session)
        return GeminiBatchProvider(session)
    if name.startswith('doubao') or name.startswith('ep-'):
        # 前端在 /connect 请求里加 "use_rtc": true 才走 RTC 路径；
        # 默认走 Batch（前端 ws 推 PCM/JPEG，跟 dashscope 完全一致）
        if session.config.get('use_rtc'):
            return DoubaoRtcProvider(session)
        return DoubaoBatchProvider(session)
    return DashScopeProvider(session)
