import os
import base64
import wave
import time
from datetime import datetime
import numpy as np

try:
    import cv2
except ImportError:
    cv2 = None

from .logger import logger

def save_debug_video_frame(video_b64, frame_index=0, kind: str = ""):
    """将 Base64 编码的视频帧保存为 JPG 图片。
    kind: 文件名后缀，例如 "compressed" 表示压缩后的帧；空表示原图。
    """
    try:
        os.makedirs('test', exist_ok=True)
        frame_bytes = base64.b64decode(video_b64)
        suffix = f"_{kind}" if kind else ""

        if cv2 and np:
            np_arr = np.frombuffer(frame_bytes, np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            if img is not None:
                utc_time = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
                fname = f"test/frame_{frame_index}_{utc_time}{suffix}.jpg"
                cv2.imwrite(fname, img)
                logger.info(f"[DEBUG] 已保存视频帧: {fname}")
            else:
                logger.warning(f"[DEBUG] 视频帧解码失败，未保存")
        else:
            utc_time = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
            fname = f"test/frame_{frame_index}_{utc_time}{suffix}.jpg"
            with open(fname, 'wb') as f:
                f.write(frame_bytes)
            logger.info(f"[DEBUG] 已保存原始视频帧: {fname}")
    except Exception as e:
        logger.error(f"[DEBUG] 视频帧保存异常: {e}")

def save_debug_audio_frame(audio_b64, audio_index=0):
    """将 Base64 编码的音频帧保存为 WAV 文件"""
    try:
        os.makedirs('test', exist_ok=True)
        audio_bytes = base64.b64decode(audio_b64)
        
        utc_time = datetime.utcnow().strftime('%Y%m%d_%H%M%S')
        fname = f"test/audio_{audio_index}_{utc_time}.wav"
        
        sample_width = 2
        sample_rate = 16000
        channels = 1
        
        with wave.open(fname, 'wb') as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(sample_rate)
            wav_file.writeframes(audio_bytes)
        
        logger.info(f"[DEBUG] 已保存音频帧: {fname}")
    except Exception as e:
        logger.error(f"[DEBUG] 音频帧保存异常: {e}")