import asyncio
import json
import time
import traceback
from urllib.parse import parse_qs, urlparse
import websockets

from ..core.session import sessions, Session
from ..core.stats import local_stats
from ..core.provider import create_provider
from ..utils.logger import logger
from ..utils.debug import save_debug_video_frame
from ..config import Settings

async def _reconnect_provider(session: Session, ws_status: str = None):
    # ----- 冷却 + 熔断 -----
    now = time.time()
    if session.reconnect_in_progress:
        logger.debug(f"[{session.session_id}] 已有重连进行中，跳过")
        return
    RECONNECT_COOLDOWN_SEC = 3.0
    SHORT_LIFE_THRESHOLD_SEC = 5.0   # 重连成功后多少秒内再断算"短命"
    MAX_CONSECUTIVE_SHORT = 3        # 连续短命多少次就熔断
    if now - session.last_reconnect_at < RECONNECT_COOLDOWN_SEC:
        logger.debug(
            f"[{session.session_id}] 重连冷却中 "
            f"({now - session.last_reconnect_at:.2f}s < {RECONNECT_COOLDOWN_SEC}s)，跳过"
        )
        return
    # 上次"重连成功"到现在如果非常短，就累计为短命；否则清零
    if (session.last_reconnect_success_at > 0
            and now - session.last_reconnect_success_at < SHORT_LIFE_THRESHOLD_SEC):
        session.consecutive_short_reconnects += 1
    else:
        session.consecutive_short_reconnects = 0
    if session.consecutive_short_reconnects >= MAX_CONSECUTIVE_SHORT:
        logger.error(
            f"[{session.session_id}] 连续 {session.consecutive_short_reconnects} 次重连后秒断，"
            f"判定为对端持续踢出（模型名/配额/Key 问题），熔断关闭会话"
        )
        try:
            if session.websocket is not None:
                await session.websocket.send(json.dumps({
                    "type": "error",
                    "message": "provider repeatedly disconnected; session aborted",
                    "timestamp": int(time.time() * 1000),
                }))
        except Exception:
            pass
        session.close()
        return

    session.reconnect_in_progress = True
    session.last_reconnect_at = now
    logger.info(f"[{session.session_id}] 开始重连 Provider ({session.config.get('provider')})")
    try:
        if session.provider:
            try:
                session.provider.close()
            except Exception:
                pass
        provider = create_provider(session, session.config.get('model') or Settings.MODEL_NAME)
        session.provider = provider
        provider.connect()
        session.last_reconnect_success_at = time.time()
        logger.info(f"[{session.session_id}] Provider 重连成功")
    except Exception as e:
        logger.error(f"[{session.session_id}] Provider 重连失败: {e}")
        session.close()
    finally:
        session.reconnect_in_progress = False

async def handle_ws(websocket):
    path = websocket.request.path
    parsed = urlparse(path)
    if not parsed.path.startswith('/v1/realtime/stream'):
        await websocket.close(1008, "Invalid path")
        return
    session_id = parse_qs(parsed.query).get('session_id', [None])[0]
    if not session_id or session_id not in sessions:
        await websocket.close(1008, "Invalid session_id")
        return
    session = sessions[session_id]
    session.websocket = websocket
    session.websocket_loop = asyncio.get_running_loop()
    logger.info(f"[{session_id}] 🔗 Agent WebSocket 已连接")
    try:
        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get('type') == 'media.stream':
                    try:
                        await do_media_stream(session, data)
                    except Exception as e:
                        logger.error(f"[{session_id}] do_media_stream异常: {e}")
                elif data.get('type') == 'commit':
                    try:
                        if hasattr(session.provider, 'commit'):
                            session.provider.commit()
                        else:
                            logger.warning(f"[{session_id}] provider 不支持 commit，忽略")
                    except Exception as e:
                        logger.error(f"[{session_id}] commit 异常: {e}")
                elif data.get('type') == 'close':
                    try:
                        session.close()
                        if session_id in sessions:
                            del sessions[session_id]
                        await websocket.send(json.dumps({"type": "closed", "session_id": session_id}))
                    except Exception as e:
                        logger.error(f"[{session_id}] 关闭会话异常: {e}")
                    return
            except json.JSONDecodeError:
                logger.warning(f"[{session_id}] 无效 JSON")
            except Exception as e:
                logger.error(f"[{session_id}] 处理消息异常：{e}")
    except websockets.exceptions.ConnectionClosed as e:
        # 【修改点】打印详细的断开代码和原因
        logger.error(f"[{session_id}] 🔌 Agent 断开 | Code: {e.code} | Reason: {e.reason}")
        
        # 可选：根据常见代码给出更具体的提示
        if e.code == 1000:
            logger.info(f"[{session_id}] 正常关闭 (Normal Closure)")
        elif e.code == 1001:
            logger.warning(f"[{session_id}] 客户端离开/页面刷新 (Going Away)")
        elif e.code == 1006:
            logger.error(f"[{session_id}] 异常断开 (Abnormal Closure) - 通常是网络中断或客户端崩溃")
        elif e.code == 1008:
            logger.warning(f"[{session_id}] 策略违规 (Policy Violation)")
        elif e.code == 1011:
            logger.error(f"[{session_id}] 服务器内部错误导致断开 (Internal Error)")
        else:
            logger.warning(f"[{session_id}] 未知断开代码: {e.code}")
    except Exception as e:
        logger.error(f"[{session_id}] WebSocket 主循环异常：{e}")
    finally:
        try:
            if not session.is_closed:
                session.close()
        except Exception as e:
            logger.error(f"[{session_id}] 最终关闭异常: {e}")
        finally:
            # 无论 close() 是否抛异常，都把 session 从全局表里摘掉，避免泄漏
            removed = sessions.pop(session_id, None)
            active = sum(
                1 for s in sessions.values()
                if s.config.get('provider') == 'dashscope' and not s.is_closed
            )
            if removed is not None:
                logger.info(
                    f"[{session_id}] 🧹 已从 sessions 移除（剩余 dashscope 活跃会话: {active}）"
                )

async def do_media_stream(session: Session, data: dict):
    audio_b64 = data.get('audio')
    video_b64 = data.get('video')

    if session.is_closed:
        return

    try:
        if session.reconnect_needed:
            await _reconnect_provider(session, "reconnect_needed flag set")
            session.reconnect_needed = False

        provider = session.provider
        if provider is None or not provider.is_alive():
            await _reconnect_provider(session, "provider not alive")
            return

        now = time.time()

        # ============ 音频限流处理 ============
        if audio_b64 and provider:
            if now - session.last_audio_time >= 0.01:
                try:
                    session.add_audio_data(audio_b64)
                    provider.append_audio(audio_b64)
                    session.audio_count += 1
                    session.last_audio_time = now
                    local_stats.add_audio()
                    if session.audio_count % 50 == 0:
                        logger.info(f"[{session.session_id}] 🎤 收到音频：{session.audio_count} 段")
                except Exception as e:
                    error_msg = str(e).lower()
                    if 'closed' in error_msg or 'connection' in error_msg:
                        logger.error(f"[{session.session_id}] 音频处理异常: 连接已关闭 - {e}")
                        await _reconnect_provider(session, None)
                        return
                    else:
                        logger.error(f"[{session.session_id}] 音频处理异常: {e}")

        # ============ 视频限流处理 (1000ms间隔) ============
        # Qwen-Omni 实时接口要求"先音频后图像"：首帧若是纯视频会报
        # invalid_request_error: Error append image before append audio，
        # 整个会话从此作废。故在收到至少一段音频前，丢弃所有视频帧。
        if video_b64 and provider and session.audio_count == 0:
            logger.debug(f"[{session.session_id}] 音频尚未开始，丢弃前置视频帧")
        elif video_b64 and provider:
            if now - session.last_video_time >= 1.0:
                if len(video_b64) > 100:
                    try:
                        # ====== 调试：保存原图 ======
                        save_debug_video_frame(video_b64, session.video_count)  # 如需关闭，注释本行
                        # ====== END ======
                        # 大小护栏：超阈值的帧在线程池里压缩，不阻塞事件循环
                        safe_b64 = await asyncio.to_thread(
                            provider.preprocess_video_b64, video_b64
                        )
                        if safe_b64 is None:
                            session.last_video_time = now  # 仍按节奏推进，避免高速重试
                        else:
                            # 调试：仅当确实做了压缩（safe_b64 ≠ 原图）时多存一份
                            if safe_b64 is not video_b64 and safe_b64 != video_b64:
                                save_debug_video_frame(
                                    safe_b64, session.video_count, kind="compressed"
                                )
                            provider.append_video(safe_b64)
                            session.video_count += 1
                            session.last_video_time = now
                            local_stats.add_video()
                            if session.video_count % 30 == 0:
                                logger.info(f"[{session.session_id}] 📹 收到视频：{session.video_count} 帧")
                    except Exception as e:
                        error_msg = str(e).lower()
                        if 'closed' in error_msg or 'connection' in error_msg:
                            logger.error(f"[{session.session_id}] 视频处理异常: 连接已关闭 - {e}")
                            await _reconnect_provider(session, None)
                            return
                        else:
                            logger.error(f"[{session.session_id}] 视频处理异常: {e}, 音频计数: {session.audio_count}")
                else:
                    logger.debug(f"[{session.session_id}] 跳过过小视频帧 (大小: {len(video_b64)} 字节)")
            else:
                logger.debug(f"[{session.session_id}] 视频帧被限流 (距离上次: {now - session.last_video_time:.2f}s)")

        # ============ 每秒统计日志 ============
        if now - local_stats.last_log_time >= 1.0:
            s = local_stats.get_stats()
            logger.info(f"[本地统计] 音频: {s['audio_frames']} 帧 | 视频: {s['video_frames']} 帧 | 总消息: {s['total_messages']} | 运行时长: {s['duration']}s")
            local_stats.last_log_time = now

    except Exception as e:
        logger.error(f"[{session.session_id}] 媒体流错误：{e}\n详细traceback:\n{traceback.format_exc()}\n音频数据存在: {audio_b64 is not None}, 视频数据存在: {video_b64 is not None}, session.is_closed: {session.is_closed}, provider: {session.provider}, 已发送音频数: {session.audio_count}")