import threading
import time

class LocalStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.audio_frames = 0
        self.video_frames = 0
        self.start_time = time.time()
        self.last_log_time = time.time()
        self.total_messages = 0

    def add_audio(self):
        with self.lock:
            self.audio_frames += 1
            self.total_messages += 1

    def add_video(self):
        with self.lock:
            self.video_frames += 1
            self.total_messages += 1

    def get_stats(self):
        with self.lock:
            duration = time.time() - self.start_time
            return {
                "audio_frames": self.audio_frames,
                "video_frames": self.video_frames,
                "total_messages": self.total_messages,
                "duration": round(duration, 2)
            }

# 全局单例
local_stats = LocalStats()