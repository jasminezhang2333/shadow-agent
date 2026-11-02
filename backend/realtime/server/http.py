import json
import os
import time
from http.server import BaseHTTPRequestHandler
from datetime import datetime
from openai import OpenAI

from ..config import Settings
from ..core.session import sessions, Session
from ..utils.logger import logger


TOOL_TRIGGER_GUARDRAIL = """

# 工具触发护栏（最高优先级，覆盖以上所有规则）
1. 当用户问的是纯识别 / 属性问询 / 视觉描述类问题时，例如：
   "这是什么"、"这是什么牌子"、"什么型号"、"哪一款"、"识别一下"、
   "看看这是"、"画面里是什么"、"这是哪款手机"，即使句中出现手机、
   电脑、品牌名等产品词，也禁止调用 comparison_prices，直接基于
   画面内容或常识回答即可。
2. 只有当用户明确表达购买或比价意图时才能调用 comparison_prices，
   判定标准是出现以下任一词义："价格 / 多少钱 / 几块"、"便宜 / 贵"、
   "买 / 购买 / 下单"、"哪里买 / 在哪买"、"比价 / 对比价格"、
   "淘宝 / 京东 / 拼多多 / 天猫"、"性价比"。
3. 第 1 条与第 2 条冲突时，一律以"禁止调用"为准。
4. 调用 comparison_prices / memory_retrieval 工具时，user_input / query
   字段必须完整复述用户原话中的产品名、型号、品牌、数量、颜色等关键信息，
   严禁用本提示词或示例中出现过的产品（如"小米14 pro"、"小米无线充电器"
   等）替换、简化或改写用户实际说的内容。例如：用户说"我想买小米17 Pro"，
   user_input 必须包含"小米17 Pro"四个字，不得改成"小米14 pro"或其他
   型号；用户说"红色的iPhone 15"，必须包含"红色"和"iPhone 15"。
   示例中的产品名仅用于说明 JSON 格式，不是默认值。
"""


class HTTPHandler(BaseHTTPRequestHandler):
    def _send_cors_headers(self):
        # 允许 file:// 与本地 demo 直接打开页面跨域调用本服务
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type, Authorization')
        self.send_header('Access-Control-Max-Age', '600')

    def _send_json(self, data: dict, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def _read_json(self):
        # 同一次请求里可能被 handle_doubao_connect 这种"先读 body 再转发"的入口
        # 提前消费过 self.rfile，这里返回缓存以避免再次读取空流。
        cached = getattr(self, '_cached_json', None)
        if cached is not None:
            self._cached_json = None
            return cached
        length = int(self.headers.get('Content-Length', 0))
        return json.loads(self.rfile.read(length).decode())

    def do_OPTIONS(self):
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()
    
    def do_POST(self):
        if self.path == '/v1/realtime/connect':
            self.handle_connect()
        elif self.path == '/v1/realtime/text':
            self.handle_text_interaction()
        elif self.path == '/v1/realtime/doubao/connect':
            self.handle_doubao_connect()
        elif self.path == '/v1/realtime/doubao/text-relay':
            self.handle_doubao_text_relay()
        else:
            self._send_json({"code": 404}, 404)

    def do_GET(self):
        if '/status' in self.path:
            sid = self.path.split('/')[4]
            self.handle_status(sid)
        else:
            self._send_json({"code": 404}, 404)

    def do_DELETE(self):
        # 兼容 /v1/realtime/session/{id} 和 /v1/realtime/doubao/session/{id}
        if '/doubao/session/' in self.path:
            sid = self.path.rsplit('/', 1)[-1]
            self.handle_doubao_delete(sid)
            return
        sid = self.path.split('/')[4]
        self.handle_delete(sid)
    
    def handle_connect(self):
        try:
            from ..core.provider import create_provider

            data = self._read_json()
            session_id = data.get('session_id')
            user_id = data.get('user_id')

            if not session_id or not user_id:
                self._send_json({"code": 400, "message": "缺少 session_id 或 user_id"}, 400)
                return

            logger.info(f"📋 创建会话：{session_id}")

            logger.info(f"data: {data}\n")

            base_instructions = data.get('instructions') or Settings.INSTRUCTIONS
            tool_descriptions = data.get('tool_descriptions') or Settings.TOOL_DESCRIPTIONS
            model_name = data.get('model') or Settings.MODEL_NAME
            provider_name = Settings.get_provider(model_name)

            if tool_descriptions and provider_name == 'dashscope':
                tool_desc_text = "\n\n可用工具列表：\n"
                for tool in tool_descriptions:
                    tool_name = tool.get('name', 'Unknown')
                    tool_desc = tool.get('description', 'No description')
                    tool_params = tool.get('parameters', {})
                    tool_desc_text += f"- {tool_name}: {tool_desc}\n"
                    properties = tool_params.get('properties', {})
                    if properties:
                        tool_desc_text += "  参数：\n"
                        for param_name, param_info in properties.items():
                            param_desc = param_info.get('description', 'No description')
                            tool_desc_text += f"    - {param_name}: {param_desc}\n"
                combined_instructions = base_instructions + tool_desc_text
            else:
                combined_instructions = base_instructions

            combined_instructions += TOOL_TRIGGER_GUARDRAIL

            logger.info("combined_instructions:\n" + combined_instructions)

            config = {
                'instructions': combined_instructions,
                'voice': data.get('voice') or Settings.VOICE,
                'enable_vad': False,
                'output_modalities': data.get('output_modalities', ['text']),
                'video_fps': data.get('video_fps', 1),
                'tool_descriptions': tool_descriptions,
                'model': model_name,
                'provider': provider_name,
                # 仅 doubao 路由有效：true=走 RTC（helper 推 PCM/JPEG 进 RTC 房间），
                # false=走 Batch（ws 推 PCM/JPEG，gummy ASR + 方舟 chat.completion）。
                # 默认 True：DoubaoRtcProvider 已改造为服务端 helper 路径，
                # agent 端无须自己接 RTC SDK。
                'use_rtc': bool(data.get('use_rtc', True)),
            }

            session = Session(session_id, user_id, config)

            # DashScope 走多 key 池：每路占一席，所有 key 都满则直接 503
            if provider_name == 'dashscope':
                from ..core.key_pool import acquire_key, pool_snapshot
                api_key = acquire_key()
                if api_key is None:
                    snap = pool_snapshot()
                    logger.warning(
                        f"[{session_id}] dashscope 全部 key 已满，拒绝建会话 snap={snap}"
                    )
                    self._send_json({
                        "code": 503,
                        "message": "所有 dashscope key 当前并发已满，请稍后再试",
                    }, 503)
                    return
                session.dashscope_api_key = api_key

            sessions[session_id] = session

            provider = create_provider(session, model_name)
            session.provider = provider
            try:
                provider.connect()
            except Exception:
                # provider 起不来，立即归还 key + 移除 session，重抛走外层错误处理
                if provider_name == 'dashscope':
                    from ..core.key_pool import release_key
                    release_key(getattr(session, 'dashscope_api_key', None))
                    session.dashscope_api_key = None
                sessions.pop(session_id, None)
                raise

            active_dashscope = sum(
                1 for s in sessions.values()
                if s.config.get('provider') == 'dashscope' and not s.is_closed
            )
            logger.info(
                f"[{session_id}] 📊 创建后活跃 dashscope 会话: {active_dashscope} "
                f"(总 sessions: {len(sessions)})"
            )

            ws_url = f"ws://127.0.0.1:8081/v1/realtime/stream?session_id={session_id}"

            resp_body = {
                "code": 200,
                "message": "连接建立成功",
                "session_id": session_id,
                "connection_status": "connected",
                "websocket_url": ws_url,
                "provider": provider_name,
                "model": model_name,
            }
            # 豆包 RTC：把房间信息（app_id/room_id/user_id/agent_user_id/token/task_id）
            # 一并下发，前端拿这些直接用火山 RTC SDK 进房；ws_url 仍保留以便走文本中继
            if provider_name == 'doubao_rtc' and config.get('use_rtc'):
                rtc_info = session.config.get('doubao_rtc') or {}
                resp_body['rtc'] = {
                    'app_id': rtc_info.get('app_id'),
                    'room_id': rtc_info.get('room_id'),
                    'user_id': rtc_info.get('user_id'),
                    'agent_user_id': rtc_info.get('agent_user_id'),
                    'token': rtc_info.get('token'),
                    'task_id': rtc_info.get('task_id'),
                }
            self._send_json(resp_body)

        except Exception as e:
            logger.error(f"创建会话失败：{e}")
            self._send_json({"code": 500, "message": str(e)}, 500)
    
    def handle_status(self, session_id):
        if session_id not in sessions:
            self._send_json({"code": 404}, 404)
            return
        s = sessions[session_id]
        self._send_json({
            "code": 200,
            "session_id": session_id,
            "status": "closed" if s.is_closed else "active",
            "connection_duration": int((datetime.now() - s.created_at).total_seconds()),
            "audio_count": s.audio_count,
            "video_count": s.video_count
        })
    
    def handle_delete(self, session_id):
        if session_id not in sessions:
            self._send_json({"code": 404}, 404)
            return
        s = sessions[session_id]
        duration = int((datetime.now() - s.created_at).total_seconds())
        s.close()
        del sessions[session_id]
        self._send_json({
            "code": 200,
            "message": "会话已关闭",
            "session_id": session_id,
            "session_duration": duration
        })
    
    def handle_text_interaction(self):
        try:
            data = self._read_json()
            session_id = data.get('session_id')
            text = data.get('text')
            default_system_prompt = """你是一个文本转发小助手，可以获得比价工具(comparison_prices)和记忆检索工具(memory_retrieval)调用的结果，你只需要透传给用户，不要胡编乱造。
            注意帮我返回TEXT最后一段文本，前面的内容是我和助理聊天的上下文（仅作为语境下的参考）。
            示例：text='[{"role": "function", "name": "memory_retrieval", "content": {"result": "在白色书桌上。"}}, {"role": "function", "name": "comparison_prices", "content": {"result": "帮你搜小米无线充电器最便宜的价格？"}}]'
            输出结果："帮你搜小米无线充电器最便宜的价格？"
            """
            system_prompt = data.get('system_prompt', default_system_prompt)
            # system_prompt = default_system_prompt
            stream = data.get('stream', False)
            # stream = False

            # 客户端可能传 dict（多语言版本，如 {'zh': '...', 'en': '...'}）。
            # 按用户文本里有没有中文字符挑一种，挑不出就回退 zh，再回退第一个 value。
            if isinstance(system_prompt, dict):
                lang = 'zh' if any('一' <= c <= '鿿' for c in (text or '')) else 'en'
                system_prompt = (
                    system_prompt.get(lang)
                    or system_prompt.get('zh')
                    or next(iter(system_prompt.values()), default_system_prompt)
                )
            if not isinstance(system_prompt, str):
                system_prompt = str(system_prompt)

            # 【新增】打印接收到的文本交互请求日志
            logger.info(f"📝 收到文本交互请求: session_id={session_id}, text='{text}', system_prompt='{system_prompt[:200]}...', stream={stream}")
            if system_prompt:
                logger.debug(f"📝 System Prompt: {system_prompt[:100]}...") # 避免日志过长，只打印前100字符

            if not session_id or not text:
                logger.warning(f"⚠️ 文本交互请求参数缺失: session_id={session_id}, text={text}")
                self._send_json({"code": 400, "message": "缺少 session_id 或 text"}, 400)
                return

            # 短路必须在 session 存在性检查之前：agent 拿到 LLM 工具调用后
            # 经常立刻断开 ws，等真的 POST /v1/realtime/text 回比价/记忆结果
            # 时 session 已经被清理。短路本身只把工具 result 转发给前端，
            # 不依赖 session 的状态，可以安全地在没 session 时也跑。
            session = sessions.get(session_id)
            # 把 text 里所有 image_generating / image_editing 的 result 喂给
            # session.image_tracker，后续 image_editing.image_to_edit /
            # image_to_3d.src_image 的 enforce 才有合法 ID 可选。
            if session is not None:
                self._record_image_ids_from_text(session, text)
            if self._maybe_short_circuit_tool_result(session, session_id, text, stream):
                return

            if session is None:
                logger.warning(f"⚠️ 会话不存在: session_id={session_id}")
                self._send_json({"code": 404, "message": "会话不存在"}, 404)
                return

            session.log_user(text)

            if stream:
                logger.info(f"🔄 开始处理流式文本响应: session_id={session_id}")
                self._handle_streaming_text_response(session, text, system_prompt)
            else:
                logger.info(f"🔄 开始处理非流式文本响应: session_id={session_id}")
                self._handle_non_streaming_text_response(session, text, system_prompt)

        except json.JSONDecodeError:
            logger.error(f"❌ JSON解析失败: 原始数据可能格式错误")
            self._send_json({"code": 400, "message": "无效的JSON格式"}, 400)
        except Exception as e:
            logger.error(f"处理文本交互请求失败：{e}", exc_info=True)
            self._send_json({"code": 500, "message": str(e)}, 500)

    # 直接透传的工具白名单：这些工具下游已经返回成品文本，
    # 不需要再过一遍 LLM 翻译。其它工具（image_generating / image_editing /
    # image_to_3d 等）必须走 LLM，因为下游返回的是 image_id 等结构化字段。
    _PASSTHROUGH_TOOL_NAMES = {"memory_retrieval", "comparison_prices"}

    def _record_image_ids_from_text(self, session, text):
        """从 /v1/realtime/text payload 里捞所有 image_generating / image_editing 的
        function result，喂给 session.image_tracker。
        """
        try:
            payload = json.loads(text) if isinstance(text, str) else text
        except Exception:
            return
        if not isinstance(payload, list):
            return
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get('role') not in ('function', 'tool'):
                continue
            try:
                session.image_tracker.record_function_result(
                    item.get('name'), item.get('content'))
            except Exception as e:
                logger.warning(
                    f"[{session.session_id}] image_tracker.record_function_result 失败: {e}"
                )

    def _maybe_short_circuit_tool_result(self, session, session_id: str, text, stream: bool) -> bool:
        """如果 text 是工具调用历史 JSON 列表，且最后一条 function 在白名单里，
        直接把 result 透传回前端，返回 True 表示已处理，调用方应跳过后续 LLM。
        其它情况返回 False。

        session 可能为 None —— agent 收到工具调用后经常立刻断开 ws，等比价/记忆
        异步返回再 POST 进来时 session 已经被清理。短路本身不依赖 session 状态。
        """
        try:
            payload = json.loads(text) if isinstance(text, str) else text
        except Exception:
            return False
        if not isinstance(payload, list) or not payload:
            return False

        # 倒序找最后一条 role=function 的项（OpenAI 兼容协议里也可能写成 'tool'）
        last_fn = None
        for item in reversed(payload):
            if not isinstance(item, dict):
                continue
            if item.get('role') in ('function', 'tool'):
                last_fn = item
                break
        if not last_fn:
            return False
        if last_fn.get('name') not in self._PASSTHROUGH_TOOL_NAMES:
            return False

        content = last_fn.get('content')
        # content 可能是 dict 或字符串化的 JSON，两种都兼容
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except Exception:
                content = {'result': content}
        if not isinstance(content, dict):
            content = {'result': str(content)}
        result_text = content.get('result', '')
        if not isinstance(result_text, str):
            result_text = json.dumps(result_text, ensure_ascii=False)

        tool_name = last_fn.get('name')
        session_state = 'alive' if session is not None else 'gone'
        logger.info(
            f"⚡ 短路工具结果直接透传: session={session_id} state={session_state} "
            f"tool={tool_name} text={result_text[:80]!r}"
        )

        # 从 payload 里捞最后一条 role=user 的原话作为本轮 user_text，
        # 这样 conversation_*.txt 里能写出"用户：xxx / AI：xxx"完整一轮
        last_user_text = ''
        for item in reversed(payload):
            if isinstance(item, dict) and item.get('role') == 'user':
                c = item.get('content', '')
                if isinstance(c, str) and c:
                    last_user_text = c
                    break

        response_id = f"resp_{int(time.time() * 1000)}"
        chat_index = int(session.chat_index) if session is not None else 1

        try:
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._send_cors_headers()
            self.end_headers()

            if stream:
                # 与 _handle_streaming_text_response 输出格式保持一致：
                # 一条 text_delta + 一条 is_final=True
                first = {
                    "code": 200,
                    "response_id": response_id,
                    "chat_index": chat_index,
                    "text_delta": result_text,
                    "is_final": False,
                    "timestamp": int(time.time() * 1000),
                }
                self.wfile.write((json.dumps(first, ensure_ascii=False) + "\n").encode('utf-8'))
                final = dict(first, text_delta="", is_final=True,
                             timestamp=int(time.time() * 1000))
                self.wfile.write((json.dumps(final, ensure_ascii=False) + "\n").encode('utf-8'))
                self.wfile.flush()
            else:
                # 非流式：与 _handle_non_streaming_text_response 一致只发一条
                obj = {
                    "code": 200,
                    "response_id": response_id,
                    "chat_index": chat_index,
                    "text": result_text,
                    "timestamp": int(time.time() * 1000),
                }
                self.wfile.write(json.dumps(obj, ensure_ascii=False).encode('utf-8'))
                self.wfile.flush()
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            logger.info(f"[{session_id}] 短路透传时客户端已断开: {e}")
            return True

        # session 还活着才记账（否则 chat_index / log_ai 没意义）
        if session is not None:
            # 让 conversation_*.txt 写出完整一轮：先 log_user 再 log_ai
            # （短路是放在 handle_text_interaction 顶部跑的，没经过 session.log_user）
            if last_user_text:
                session.log_user(last_user_text)
            session.log_ai(result_text)
            chat_index_after = session.chat_index
            session.chat_index += 1
        else:
            chat_index_after = chat_index

        # 与 _handle_streaming_text_response 末尾对齐的完成日志，便于 grep
        logger.info(
            f"✅ 短路响应完成 (session_id={session_id}, "
            f"chat_index={chat_index_after}, tool={tool_name}, mode={'stream' if stream else 'sync'}): "
            f"{result_text[:100]}{'...' if len(result_text) > 100 else ''}"
        )
        return True

    def _handle_streaming_text_response(self, session, text, system_prompt=None):
        """
        处理流式响应，按照用户指定的 JSON 格式逐块发送。
        模型 token 先进 sentence_buffer，遇到任意标点（强结束符或弱分隔符）就切一段下发；
        若整段没有标点会等到结束 force_all 一次性发出。
        """
        response_id = f"resp_{int(time.time() * 1000)}"

        current_chat_index = int(session.chat_index)

        # 句子缓冲切片参数（与 callback.py 一致：只用标点切，不再设字符数上限/下限）
        STRONG_ENDINGS = set('。.！？!?\n')
        WEAK_SPLITTERS = set(',，""<>;；:：')
        SPLIT_CHARS = STRONG_ENDINGS | WEAK_SPLITTERS
        sentence_buffer = ""

        def _emit(text_delta: str, is_final: bool):
            obj = {
                "code": 200,
                "response_id": response_id,
                "chat_index": current_chat_index,
                "text_delta": text_delta,
                "is_final": is_final,
                "timestamp": int(time.time() * 1000),
            }
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode('utf-8'))
            self.wfile.flush()

        def _flush_buffer(force_all: bool = False) -> bool:
            """从 sentence_buffer 切一段发出去；返回是否真的发了。"""
            nonlocal sentence_buffer
            if not sentence_buffer:
                return False
            if force_all:
                text_to_send = sentence_buffer
                sentence_buffer = ""
            else:
                split_pos = -1
                for i, ch in enumerate(sentence_buffer):
                    if ch in SPLIT_CHARS:
                        split_pos = i + 1
                        break
                if split_pos == -1:
                    return False
                text_to_send = sentence_buffer[:split_pos]
                sentence_buffer = sentence_buffer[split_pos:]
            _emit(text_to_send, is_final=False)
            return True

        try:
            client = OpenAI(
                api_key=os.getenv('DASHSCOPE_API_KEY', Settings.DASHSCOPE_API_KEY),
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": text})

            logger.info(f"🚀 正在调用模型获取流式响应... (session_id={session.session_id})")

            completion = client.chat.completions.create(
                model="qwen3-omni-flash",
                messages=messages,
                stream=True,
                modalities=["text"],
                extra_body={'enable_thinking': False},
                stream_options={"include_usage": False}
            )

            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self._send_cors_headers()
            self.end_headers()

            full_response_text = ""

            for chunk in completion:
                try:
                    if not (chunk.choices and chunk.choices[0].delta.content):
                        continue
                    content = chunk.choices[0].delta.content
                    full_response_text += content
                    sentence_buffer += content
                    # 只要 buffer 里还有标点就一直切；没有标点就继续累积
                    while any(ch in SPLIT_CHARS for ch in sentence_buffer):
                        prev = len(sentence_buffer)
                        if not _flush_buffer(force_all=False):
                            break
                        if len(sentence_buffer) == prev:
                            break
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
                    logger.info(f"[{session.session_id}] 客户端在流式传输期间断开连接: {e}")
                    return
                except Exception as e:
                    logger.error(f"[{session.session_id}] 流式写入异常: {e}")
                    return

            # 模型流结束：把残留 buffer 一次性 flush，然后发 is_final
            try:
                if sentence_buffer:
                    _flush_buffer(force_all=True)
                _emit("", is_final=True)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                logger.info(f"[{session.session_id}] 客户端在发送最终标记前断开连接")
                return

            session.log_ai(full_response_text)
            logger.info(f"✅ 流式响应完成 (session_id={session.session_id}, chat_index={current_chat_index}): {full_response_text[:100]}...")

            session.chat_index += 1

        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError) as e:
            logger.info(f"[{session.session_id}] 连接已中止: {e}")
        except Exception as e:
            logger.error(f"流式文本响应失败：{e}", exc_info=True)
            try:
                error_obj = {
                    "code": 500,
                    "response_id": response_id,
                    "chat_index": current_chat_index,
                    "text_delta": f"Error: {str(e)}",
                    "is_final": True,
                    "timestamp": int(time.time() * 1000)
                }
                self.wfile.write(json.dumps(error_obj, ensure_ascii=False).encode('utf-8'))
                self.wfile.flush()
            except:
                pass
    def _handle_non_streaming_text_response(self, session, text, system_prompt=None):
        try:
            client = OpenAI(
                api_key=os.getenv('DASHSCOPE_API_KEY', Settings.DASHSCOPE_API_KEY),
                base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
            )

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": text})

            logger.info(f"🚀 正在调用模型获取响应... (session_id={session.session_id})")
            
            completion = client.chat.completions.create(
                model="qwen3-omni-flash",
                messages=messages,
                extra_body={'enable_thinking': False},
                modalities=["text"],
            )

            response_id = f"resp_{int(time.time() * 1000)}"
            response_text = completion.choices[0].message.content if completion.choices else "未能获取模型响应"
            
            # 【新增】打印响应内容日志
            logger.info(f"✅ 模型响应完成 (session_id={session.session_id}): {response_text}")
            
            usage_info = completion.usage if hasattr(completion, 'usage') else None
            input_tokens = getattr(usage_info, 'input_tokens', 0) if usage_info else 0
            output_tokens = getattr(usage_info, 'output_tokens', 0) if usage_info else 0
            total_tokens = getattr(usage_info, 'total_tokens', 0) if usage_info else 0

            result = {
                "code": 200,
                "response_id": response_id,
                "response": response_text,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "total_tokens": total_tokens
                },
                "timestamp": int(time.time() * 1000)
            }

            session.log_ai(response_text)
            self._send_json(result)

        except Exception as e:
            logger.error(f"非流式文本响应失败：{e}", exc_info=True)
            self._send_json({"code": 500, "message": str(e)}, 500)
    
    def log_message(self, format, *args):
        pass

    # ---------------------------------------------------------------
    # Doubao RTC 路由
    # ---------------------------------------------------------------
    def handle_doubao_connect(self):
        """兼容老入口：把 /v1/realtime/doubao/connect 重定向到统一的 handle_connect。
        老前端 demo 调这条路径不传 model 也不传 use_rtc，这里自动补默认值，
        让它继续走 RTC 路径（doubao_rtc_demo.html 里有真实的 RTC 入房逻辑）。
        """
        try:
            data = self._read_json()
        except Exception:
            data = {}
        if not data.get('model'):
            data['model'] = Settings.DOUBAO_ENDPOINT_ID
        # 老 demo 完全围绕 RTC 设计，必须保留 use_rtc=True
        data.setdefault('use_rtc', True)

        self._cached_json = data
        self.handle_connect()

    def handle_doubao_text_relay(self):
        """方案 Y：客户端从 RTC 数据通道收到 LLM 文本后 POST 给本服务。
        服务这里做一致性日志、工具调用解析，然后通过现有 ws 转发给 Agent。
        """
        try:
            data = self._read_json()
            session_id = data.get('session_id')
            text_delta = data.get('text_delta', '')
            is_final = bool(data.get('is_final', False))
            response_id = data.get('response_id') or f"resp_{int(time.time()*1000)}"

            if not session_id or session_id not in sessions:
                self._send_json({"code": 404, "message": "会话不存在"}, 404)
                return
            session = sessions[session_id]

            payload = {
                "type": "response",
                "response_id": response_id,
                "text_delta": text_delta,
                "is_final": is_final,
                "tool_call": data.get('tool_call', {}),
                "timestamp": int(time.time() * 1000),
                "chat_index": getattr(session, 'chat_index', 1),
            }

            ws = session.websocket
            loop = session.websocket_loop
            if ws is not None and loop is not None:
                import asyncio
                asyncio.run_coroutine_threadsafe(
                    ws.send(json.dumps(payload, ensure_ascii=False)),
                    loop,
                )

            if is_final and text_delta:
                try:
                    session.log_ai(text_delta)
                except Exception:
                    pass

            self._send_json({"code": 200, "message": "ok"})
        except Exception as e:
            logger.error(f"[doubao] text-relay 失败: {e}", exc_info=True)
            self._send_json({"code": 500, "message": str(e)}, 500)

    def handle_doubao_delete(self, session_id: str):
        """兼容老路径：旧 demo 调 DELETE /v1/realtime/doubao/session/{id}。
        现在统一走 handle_delete，由 session.close() → provider.close() 触发 stop_voice_chat。
        """
        self.handle_delete(session_id)