import os
import wave
import time
import base64
import threading
from datetime import datetime
from typing import Optional, Dict

from ..utils.logger import logger, LOGS_DIR

sessions: Dict[str, 'Session'] = {}
_event_loop = None

def get_event_loop():
    global _event_loop
    if _event_loop is None:
        import asyncio
        _event_loop = asyncio.new_event_loop()
        threading.Thread(target=_event_loop.run_forever, daemon=True).start()
    return _event_loop

class Session:
    def __init__(self, session_id: str, user_id: str, config: dict):
        self.session_id = session_id
        self.user_id = user_id
        self.config = config
        self.websocket = None
        self.websocket_loop = None
        self.conversation = None
        self.provider = None
        self.callback = None
        self.is_closed = False
        self.created_at = datetime.now()
        self.audio_count = 0
        self.video_count = 0
        
        self.round = 0
        self.last_user = None
        self.last_ai = None
        self.last_user_time = None
        self.last_ai_time = None
        self.last_latency = None
        self.log_file = os.path.join(LOGS_DIR, f"conversation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
        
        self.audio_buffer = bytearray()
        self.audio_lock = threading.Lock()
        
        # 限流时间戳
        self.last_audio_time = 0  # 上次处理音频的时间
        self.last_video_time = 0  # 上次处理视频的时间
        
        self.reconnect_needed = False  # 是否需要重连DashScope
        self.chat_index = 1

        # 图片 ID 追踪：image_generating / image_editing 工具的 result 累积成列表，
        # 校验 image_editing.image_to_edit 和 image_to_3d.src_image 必须在列表里。
        from .image_id_tracker import ImageIdTracker
        self.image_tracker = ImageIdTracker(session_id=self.session_id)

        # --- 重连节流，防止 provider 秒断时雪崩重连 ---
        self.last_reconnect_at = 0.0          # 上次重连发起的时间戳（time.time()）
        self.reconnect_in_progress = False    # 是否有重连正在执行
        self.consecutive_short_reconnects = 0  # 连续“连上后很快又断”的次数
        self.last_reconnect_success_at = 0.0   # 上次重连成功完成的时间戳

        # 多 key 池：每个 dashscope session 占一席，close() 时归还
        self.dashscope_api_key: Optional[str] = None

        # 硬关键词路由：本轮已经命中过的话，记下当前 round 号，
        # 让 callback 把 LLM 文本/工具增量丢弃。详见 tool_keyword_router.py。
        self._kw_tool_fired_round: Optional[int] = None

    def close(self):
        if self.is_closed:
            return
        self.is_closed = True
        # self._save_session_audio()
        if self.provider:
            try:
                self.provider.close()
            except Exception as e:
                logger.error(f"关闭 provider 错误：{e}")
        if self.conversation:
            try:
                self.conversation.close()
            except Exception as e:
                logger.error(f"关闭错误：{e}")
        # 归还 dashscope key 槽位（幂等，只归一次）
        if self.dashscope_api_key:
            try:
                from .key_pool import release_key
                release_key(self.dashscope_api_key)
            except Exception as e:
                logger.error(f"归还 dashscope key 异常：{e}")
            finally:
                self.dashscope_api_key = None

    def add_audio_data(self, audio_b64: str):
        try:
            audio_bytes = base64.b64decode(audio_b64)
            with self.audio_lock:
                self.audio_buffer.extend(audio_bytes)
        except Exception as e:
            logger.error(f"[{self.session_id}] 添加音频数据异常: {e}")

    def _save_session_audio(self):
        try:
            with self.audio_lock:
                if len(self.audio_buffer) == 0:
                    return
                
                os.makedirs('test', exist_ok=True)
                utc_time = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
                fname = f"test/audio_session_{self.session_id}_{utc_time}.wav"
                
                sample_width = 2
                sample_rate = 24000
                channels = 1
                
                with wave.open(fname, 'wb') as wav_file:
                    wav_file.setnchannels(channels)
                    wav_file.setsampwidth(sample_width)
                    wav_file.setframerate(sample_rate)
                    wav_file.writeframes(self.audio_buffer)
                
                logger.info(f"[{self.session_id}] 已保存会话音频: {fname}")
        except Exception as e:
            logger.error(f"[{self.session_id}] 保存会话音频异常: {e}")

    def log_user(self, user_text):
        self.round += 1
        self.last_user = user_text
        self.last_user_time = datetime.now()

    def log_ai(self, ai_text):
        self.last_ai = ai_text
        self.last_ai_time = datetime.now()
        if self.last_user_time and self.last_ai_time:
            self.last_latency = int((self.last_ai_time - self.last_user_time).total_seconds() * 1000)
            # 统一延迟打点：用户说完一句到模型本轮回包完成的端到端延迟
            # provider 字段从 config 读，方便 grep 各模型；轮次号方便对账
            provider = (self.config or {}).get('provider', '?')
            preview = (ai_text or '').replace('\n', ' ')[:60]
            logger.info(
                f"[latency] session={self.session_id} provider={provider} "
                f"round={self.round} e2e={self.last_latency}ms ai={preview!r}"
            )
        self._write_round_log()

    def _write_round_log(self):
        # AI 有回复就落盘；用户那一行如果 transcription 没到，写占位符
        if self.last_ai is None:
            return
        if self.last_user is None:
            self.round += 1
        ai_time_str = self.last_ai_time.strftime('%H:%M:%S') if self.last_ai_time else '--:--:--'
        user_time_str = self.last_user_time.strftime('%H:%M:%S') if self.last_user_time else ai_time_str
        user_line = self.last_user if self.last_user is not None else '[无转写]'
        log_lines = [
            f"[第{self.round}轮] {user_time_str}",
            f"用户：{user_line}",
            f"AI: {self.last_ai}",
            "------------------------------------------------------------",
            f"延迟：{self.last_latency if self.last_latency is not None else '-'}ms",
            ""
        ]
        with open(self.log_file, 'a', encoding='utf-8') as f:
            f.write('\n'.join(log_lines))
        self.last_user = None
        self.last_ai = None
        self.last_user_time = None
        self.last_ai_time = None
        self.last_latency = None