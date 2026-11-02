"""
Doubao RTC 控制平面：StartVoiceChat / StopVoiceChat / UpdateVoiceChat。

不接入 BaseProvider 抽象 —— 因为媒体流走 RTC，本服务不持有 append_audio/append_video。
本模块只做控制平面调用 + RTC Token 生成，让客户端拿到房间信息直接进 RTC。

OpenAPI 调用全程交给火山官方 volcengine SDK 的 Service 工具签名 + 发送，避免自签出现
SignatureDoesNotMatch。RTC Token 暂时按公开协议拼，若失败需要换用官方 token 工具。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
import uuid
from typing import Optional

from ..config import Settings
from ..utils.logger import logger


# ---------------------------------------------------------------------------
# OpenAPI 客户端（基于官方 SDK）
# ---------------------------------------------------------------------------

_service_singleton = None


def _get_service():
    """惰性构造一个 volcengine.Service 实例，复用其内置签名 + 重试 + 编码。"""
    global _service_singleton
    if _service_singleton is not None:
        return _service_singleton

    from volcengine.ApiInfo import ApiInfo
    from volcengine.ServiceInfo import ServiceInfo
    from volcengine.base.Service import Service
    from volcengine.Credentials import Credentials
    from urllib.parse import urlparse

    parsed = urlparse(Settings.VOLC_RTC_OPENAPI)
    host = parsed.netloc or 'rtc.volcengineapi.com'
    scheme = parsed.scheme or 'https'

    sinfo = ServiceInfo(
        host, {},
        Credentials(Settings.VOLC_AK, Settings.VOLC_SK, 'rtc', Settings.VOLC_REGION),
        15, 15, scheme,
    )
    actions = ['StartVoiceChat', 'UpdateVoiceChat', 'StopVoiceChat']
    api_infos = {
        a: ApiInfo('POST', '/',
                   {'Action': a, 'Version': Settings.VOLC_RTC_API_VERSION},
                   {}, {})
        for a in actions
    }

    class _RtcService(Service):
        def __init__(self):
            super().__init__(sinfo, api_infos)

    _service_singleton = _RtcService()
    return _service_singleton


def _post_openapi(action: str, payload: dict) -> dict:
    svc = _get_service()
    body = json.dumps(payload, ensure_ascii=False)
    raw = svc.json(action, {}, body)
    try:
        data = json.loads(raw)
    except Exception:
        raise RuntimeError(f"{action} 返回非 JSON: {raw[:300] if raw else raw!r}")
    err = (data.get('ResponseMetadata') or {}).get('Error')
    if err:
        raise RuntimeError(f"{action} 失败: err={err}")
    return data


def start_voice_chat(*, room_id: str, task_id: str,
                     instructions: str,
                     agent_user_id: str,
                     target_user_id: str,
                     welcome_message: Optional[str] = None) -> dict:
    """启动 RTC AI Agent。Agent 以 agent_user_id 加入 room_id 房间，
    订阅 target_user_id 的音视频流。

    返回 OpenAPI 原始响应（含 TaskId、AgentInfo 等）。
    """
    llm_cfg = {
        'Mode': 'ArkV3',
        'EndPointId': Settings.DOUBAO_ENDPOINT_ID,
        'ModelName': Settings.DOUBAO_ENDPOINT_ID,  # 兼容直接用模型名的场景
        'SystemMessages': [instructions] if instructions else [],
        'MaxTokens': Settings.DOUBAO_LLM_MAX_TOKENS,
        'Temperature': Settings.DOUBAO_LLM_TEMPERATURE,
        'EnableSubtitle': True,
    }
    if Settings.DOUBAO_ENABLE_VISION:
        llm_cfg['VisionConfig'] = {
            'Enable': True,
            'SnapshotConfig': {
                'StreamType': Settings.DOUBAO_VISION_STREAM_TYPE,
                'ImageDetail': Settings.DOUBAO_VISION_IMAGE_DETAIL,
                'Height': Settings.DOUBAO_VISION_HEIGHT,
                'Interval': Settings.DOUBAO_VISION_INTERVAL_MS,
                'ImagesLimit': Settings.DOUBAO_VISION_IMAGES_LIMIT,
            },
        }
    # ASR / TTS 用「语音技术」AppID（不是 RTC AppID），AccessToken 是真正鉴权
    asr_cfg = {
        'Provider': Settings.DOUBAO_ASR_PROVIDER,
        'ProviderParams': {
            'AppId': Settings.VOLC_SPEECH_APP_ID,
            'AccessToken': Settings.VOLC_SPEECH_TOKEN,
            'Cluster': Settings.DOUBAO_ASR_CLUSTER,
            # 缩短静音判停 → ASR 更早出 definite=true → LLM 更早开始流式生成
            'EndWindowSize': Settings.DOUBAO_ASR_END_WINDOW_MS,
        },
        'VolcAsrParams': {
            # 火山通用版 ASR 提供的 VAD 参数。两边都填一份，兼容不同版本字段名。
            'EndWindowSize': Settings.DOUBAO_ASR_END_WINDOW_MS,
        },
    }
    config = {
        'LLMConfig': llm_cfg,
        'ASRConfig': asr_cfg,
        # 数据通道字幕：让 Agent 把 LLM 文本通过 RTC OnRoomBinaryMessageReceived 推回
        'SubtitleConfig': {'SubtitleMode': 1, 'EnableSubtitle': True},
        # InterruptMode=1 允许用户随时打断 Agent，Agent 也会在用户短停顿时
        # 提前开口，对话节奏明显比 InterruptMode=0 (必须等整段说完) 快。
        'InterruptMode': Settings.DOUBAO_INTERRUPT_MODE,
    }
    if Settings.DOUBAO_ENABLE_TTS:
        config['TTSConfig'] = {
            'Provider': Settings.DOUBAO_TTS_PROVIDER,
            'ProviderParams': {
                'AppId': Settings.VOLC_SPEECH_APP_ID,
                'AccessToken': Settings.VOLC_SPEECH_TOKEN,
                'Cluster': Settings.DOUBAO_TTS_CLUSTER,
                'VoiceType': Settings.DOUBAO_TTS_VOICE,
            },
        }

    payload = {
        'AppId': Settings.VOLC_APP_ID,
        'RoomId': room_id,
        'TaskId': task_id,
        'AgentConfig': {
            'UserId': agent_user_id,
            'WelcomeMessage': welcome_message or '',
            'TargetUserId': [target_user_id],  # 必须为单元素数组，火山会校验
            'EnableConversationStateCallback': False,
        },
        'Config': config,
    }
    logger.info(
        f"[doubao] StartVoiceChat room={room_id} task={task_id} "
        f"agent={agent_user_id} endpoint={Settings.DOUBAO_ENDPOINT_ID} "
        f"vision={'on' if Settings.DOUBAO_ENABLE_VISION else 'off'} "
        f"interval={Settings.DOUBAO_VISION_INTERVAL_MS}ms "
        f"limit={Settings.DOUBAO_VISION_IMAGES_LIMIT}"
    )
    return _post_openapi('StartVoiceChat', payload)


def stop_voice_chat(*, room_id: str, task_id: str) -> dict:
    payload = {
        'AppId': Settings.VOLC_APP_ID,
        'RoomId': room_id,
        'TaskId': task_id,
    }
    logger.info(f"[doubao] StopVoiceChat room={room_id} task={task_id}")
    return _post_openapi('StopVoiceChat', payload)


def update_voice_chat(*, room_id: str, task_id: str,
                      command: str, message: Optional[str] = None) -> dict:
    """命令：interrupt / external-text / external-audio 等，按文档枚举。"""
    payload = {
        'AppId': Settings.VOLC_APP_ID,
        'RoomId': room_id,
        'TaskId': task_id,
        'Command': command,
    }
    if message is not None:
        payload['Message'] = message
    logger.info(f"[doubao] UpdateVoiceChat cmd={command} room={room_id}")
    return _post_openapi('UpdateVoiceChat', payload)


# ---------------------------------------------------------------------------
# RTC Token 生成（客户端进房间用）
# ---------------------------------------------------------------------------
# 火山官方 AccessToken v3 算法（与 https://www.volcengine.com/docs/6348/70121
# 提供的 RTC_Token.zip 内 Python 实现一致）：
#   raw_str = "001" + app_id_string(原样字符串，不做 base64) + base64(packed)
#   packed  = pack_bytes(msg) + pack_bytes(signature)
#   msg     = u32(nonce) + u32(issued_at) + u32(expire_at)
#             + pack_string(room_id) + pack_string(user_id)
#             + pack_map_u32(privileges)
#   signature = HMAC-SHA256(app_key, msg)
# 全部小端编码；pack_bytes / pack_string 前缀为 u16 长度。

_TOKEN_VERSION = "001"

# 权限位（与官方常量一致）
_PRIV_PUBLISH_STREAM = 0
_PRIV_PUBLISH_AUDIO = 1
_PRIV_PUBLISH_VIDEO = 2
_PRIV_PUBLISH_DATA = 3
_PRIV_SUBSCRIBE_STREAM = 4


def _pack_u16(v: int) -> bytes:
    return struct.pack('<H', int(v) & 0xFFFF)


def _pack_u32(v: int) -> bytes:
    return struct.pack('<I', int(v) & 0xFFFFFFFF)


def _pack_bytes(b: bytes) -> bytes:
    return _pack_u16(len(b)) + b


def _pack_string(s: str) -> bytes:
    return _pack_bytes(s.encode('utf-8'))


def _pack_map_u32(m: dict) -> bytes:
    items = sorted(((int(k), int(v)) for k, v in m.items()), key=lambda kv: kv[0])
    out = bytearray()
    out.extend(_pack_u16(len(items)))
    for k, v in items:
        out.extend(_pack_u16(k))
        out.extend(_pack_u32(v))
    return bytes(out)


def generate_rtc_token(*, app_id: str, app_key: str,
                       room_id: str, user_id: str,
                       expire_seconds: int = 3600) -> str:
    """生成火山 RTC AccessToken v3。

    app_key 必须是 RTC 控制台的 AppKey（与 AppID 同处显示），
    不是 IAM 用户的 SecretAccessKey。
    """
    import random as _rand
    issued_at = int(time.time())
    expire_at = issued_at + int(expire_seconds)
    # 官方实现用 random.randint(1, 99999999)；保持一致
    nonce = _rand.randint(1, 99999999)

    # 默认授予订阅 + 发布（4 个发布子权限），过期时间和 token 同寿
    privileges = {
        _PRIV_SUBSCRIBE_STREAM: expire_at,
        _PRIV_PUBLISH_STREAM: expire_at,
        _PRIV_PUBLISH_AUDIO: expire_at,
        _PRIV_PUBLISH_VIDEO: expire_at,
        _PRIV_PUBLISH_DATA: expire_at,
    }

    msg = bytearray()
    msg.extend(_pack_u32(nonce))
    msg.extend(_pack_u32(issued_at))
    msg.extend(_pack_u32(expire_at))
    msg.extend(_pack_string(room_id))
    msg.extend(_pack_string(user_id))
    msg.extend(_pack_map_u32(privileges))

    signature = hmac.new(app_key.encode('utf-8'), bytes(msg),
                         hashlib.sha256).digest()
    content = _pack_bytes(bytes(msg)) + _pack_bytes(signature)
    return _TOKEN_VERSION + app_id + base64.b64encode(content).decode('ascii')


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def new_task_id(prefix: str = 'task') -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def derive_agent_user_id(session_id: str) -> str:
    return f"agent_{session_id[-12:]}"


def derive_human_user_id(session_id: str) -> str:
    return f"user_{session_id[-12:]}"
