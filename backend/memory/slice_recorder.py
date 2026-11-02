"""40s 记忆切片录制器：把 Android 推上来的 JPEG 帧 + PCM 音频攒够 40 秒，
用 ffmpeg 合成一段本地 mp4，交给后台 worker 异步抽记忆。

为什么在后端合成而不是让 Android 录 mp4：
- Android 端已经在以 ~1fps 推 JPEG + 16k/16bit/mono PCM（见 MicRecorder / CaptureService），
  复用这条现成的流，不动客户端。代价：视频是 1~2fps 拼接、音画只做粗对齐，
  对「离线抽记忆」这种不要求精确同步的场景够用。

命名约定对齐 video-memory 的 parse_video_id_day_time：
    DAY1_shadow_<HHMMSSFF>.mp4
  → day=DAY1, time_code=HHMMSSFF（时分秒+2 位帧号，这里帧号固定 00）
  让抽出来的记忆带上 day/time_code，检索端 rerank 的 recency 才有意义。
"""
from __future__ import annotations

import os
import time
import shutil
import tempfile
import subprocess
from datetime import datetime
from typing import Awaitable, Callable

import asyncio

FFMPEG = os.getenv("FFMPEG_BIN", "ffmpeg")

# 音频格式：和 Android MicRecorder 一致（16kHz / 16bit / mono）
AUDIO_RATE = 16000
AUDIO_BYTES_PER_SEC = AUDIO_RATE * 2  # 16bit = 2 bytes/sample, mono


class SliceRecorder:
    """每个 WebSocket 连接一个实例；非线程安全，只在单连接的事件循环里喂。"""

    def __init__(self, slices_dir: str,
                 enqueue: Callable[[str], Awaitable[None]],
                 slice_seconds: float = 40.0,
                 day: str = "DAY1"):
        self.slices_dir = slices_dir
        self.enqueue = enqueue
        self.slice_seconds = slice_seconds
        self.day = day
        os.makedirs(slices_dir, exist_ok=True)
        self._reset()

    def _reset(self) -> None:
        self._frames: list[bytes] = []   # 每个元素是一帧完整 JPEG
        self._pcm = bytearray()
        self._start = time.monotonic()

    # ---------- 喂数据 ----------
    def add_video(self, jpeg: bytes) -> None:
        if jpeg:
            self._frames.append(jpeg)

    def add_audio(self, pcm: bytes) -> None:
        if pcm:
            self._pcm.extend(pcm)

    def due(self) -> bool:
        return (time.monotonic() - self._start) >= self.slice_seconds

    def _has_content(self) -> bool:
        return bool(self._frames) or bool(self._pcm)

    # ---------- 切片 ----------
    async def maybe_flush(self) -> None:
        """攒够 40s 就合成一段并入队；没内容则跳过。"""
        if self.due() and self._has_content():
            await self.flush()

    async def flush(self) -> None:
        if not self._has_content():
            self._reset()
            return
        frames, pcm = self._frames, bytes(self._pcm)
        self._reset()  # 立刻清空，继续攒下一段，合成放到线程里
        out_path = await asyncio.to_thread(self._synthesize, frames, pcm)
        if out_path:
            await self.enqueue(out_path)

    def _synthesize(self, frames: list[bytes], pcm: bytes) -> str | None:
        """用 ffmpeg 把 JPEG 序列 + PCM 合成一段 mp4，返回路径；失败返回 None。"""
        now = datetime.now()
        time_code = f"{now.hour:02d}{now.minute:02d}{now.second:02d}00"
        name = f"{self.day}_shadow_{time_code}.mp4"
        out_path = os.path.join(self.slices_dir, name)

        tmp = tempfile.mkdtemp(prefix="slice_")
        try:
            # 视频帧率：让视频时长 ≈ 音频时长（粗对齐）
            audio_sec = len(pcm) / AUDIO_BYTES_PER_SEC if pcm else self.slice_seconds
            audio_sec = max(audio_sec, 1.0)
            n_frames = len(frames)

            cmd = [FFMPEG, "-y"]
            if n_frames > 0:
                for i, jpg in enumerate(frames):
                    with open(os.path.join(tmp, f"frame_{i:05d}.jpg"), "wb") as f:
                        f.write(jpg)
                fps = max(n_frames / audio_sec, 0.1)
                cmd += ["-framerate", f"{fps:.4f}",
                        "-i", os.path.join(tmp, "frame_%05d.jpg")]
            if pcm:
                pcm_path = os.path.join(tmp, "audio.pcm")
                with open(pcm_path, "wb") as f:
                    f.write(pcm)
                cmd += ["-f", "s16le", "-ar", str(AUDIO_RATE), "-ac", "1",
                        "-i", pcm_path]

            if n_frames > 0:
                cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
            if pcm:
                cmd += ["-c:a", "aac"]
            cmd += ["-shortest", out_path]

            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if proc.returncode != 0 or not os.path.exists(out_path):
                return None
            return out_path
        except Exception:
            return None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
