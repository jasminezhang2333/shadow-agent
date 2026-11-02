import json
import time
import ast
import asyncio
import re
from dashscope.audio.qwen_omni import OmniRealtimeCallback

from .session import Session, get_event_loop
from .tool_keyword_router import try_fire_keyword_tool, is_round_handled
from ..utils.logger import logger


# 已知的"通用包装"标签名（标签内是完整 JSON tool call）
_GENERIC_WRAPPER_TAGS = {'tool_code', 'toolcall', 'function', 'tool_call', 'tool_use'}

# 任意 XML 风格起始标签：<name> 或 <name attr="x">
_ANY_OPEN_TAG_RE = re.compile(r'<\s*([A-Za-z_][\w\-]*)\s*[^>]*>', re.DOTALL)

# 工具调用 JSON 特征：{"name":  /  {'name':  （允许中间空白）
_TOOL_CALL_JSON_RE = re.compile(r'\{\s*["\']?name["\']?\s*:', re.DOTALL)


def _looks_like_tool_call_start(stripped: str) -> bool:
    """判断流式累积文本看起来是不是工具调用的起始。

    覆盖四种情况：
    1. 直接 JSON：`{...}`
    2. 通用包装：`<tool_code>...`、`<toolcall>...`、`<function>...` 等
    3. 标签即工具名：`<memory_retrieval>...`、`<image_generating>...` 等
    4. 自然语言前缀 + JSON：模型先吐了一句话，再吐 `{'name': ...}`
       （只要累加文本任意位置出现 `{"name":` / `{'name':` 就按工具调用对待）
    """
    if not stripped:
        return False
    if stripped.startswith('{'):
        return True
    if _ANY_OPEN_TAG_RE.match(stripped):
        return True
    return bool(_TOOL_CALL_JSON_RE.search(stripped))


def _extract_tool_call_substring(raw: str):
    """从混合文本中抽出第一个看起来像 tool call 的平衡 {...} 子串。

    用于处理模型在 JSON 前面加了自然语言前缀的情况，例如：
        "好的，我来帮你。 {'name': 'image_generating', 'arguments': {...}}"
    """
    m = _TOOL_CALL_JSON_RE.search(raw)
    if not m:
        return None
    start = m.start()
    depth = 0
    in_str = False
    str_char = None
    escape = False
    for i in range(start, len(raw)):
        c = raw[i]
        if in_str:
            if escape:
                escape = False
            elif c == '\\':
                escape = True
            elif c == str_char:
                in_str = False
            continue
        if c == '"' or c == "'":
            in_str = True
            str_char = c
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return raw[start:i + 1]
    return None


def _coerce_arguments(args):
    """arguments 可能是 dict / JSON 字符串 / Python 字面量字符串，统一成 dict。"""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            return json.loads(args)
        except Exception:
            try:
                return ast.literal_eval(args)
            except Exception:
                return args
    return args


def _parse_tool_call_text(raw_content: str):
    """把模型输出的工具调用原文解析成 {'name', 'arguments', ...} dict。

    支持三种形态：
    A. 通用包装：<tool_code>{"name": "...", "arguments": {...}}</tool_code>
    B. 标签即工具名：<memory_retrieval>{"query": "..."}</memory_retrieval>
       → 视为 {"name": "memory_retrieval", "arguments": {"query": "..."}}
    C. 裸 JSON / Python dict：{'name': '...', 'arguments': {...}}

    解析失败返回 None。
    """
    if not raw_content:
        return None
    raw = raw_content.strip()

    # 先尝试匹配最外层 XML 标签
    open_match = _ANY_OPEN_TAG_RE.match(raw)
    if open_match:
        tag = open_match.group(1)
        # 找对应闭合标签的内容（容忍大小写与空白）
        close_re = re.compile(
            r'<\s*/\s*' + re.escape(tag) + r'\s*>', re.IGNORECASE)
        close_match = close_re.search(raw, open_match.end())
        inner = raw[open_match.end():close_match.start()].strip() if close_match \
            else raw[open_match.end():].strip()

        inner_obj = _try_parse_json_or_literal(inner)

        if tag.lower() in _GENERIC_WRAPPER_TAGS:
            # 通用包装：内层必须是带 name 的 dict
            if isinstance(inner_obj, dict) and 'name' in inner_obj:
                inner_obj['arguments'] = _coerce_arguments(inner_obj.get('arguments', {}))
                return inner_obj
            return None

        # 标签即工具名：内层一般是 arguments 字典
        if isinstance(inner_obj, dict):
            if 'name' in inner_obj and 'arguments' in inner_obj:
                # 模型把整个 tool call 又塞进了同名标签里，去壳
                inner_obj['arguments'] = _coerce_arguments(inner_obj.get('arguments', {}))
                return inner_obj
            return {'name': tag, 'arguments': _coerce_arguments(inner_obj)}
        # 内层不是 dict 就当空参数
        return {'name': tag, 'arguments': {}}

    # 没有 XML 标签：当作裸 JSON / Python dict
    parsed = _try_parse_json_or_literal(raw)
    if isinstance(parsed, dict) and 'name' in parsed:
        parsed['arguments'] = _coerce_arguments(parsed.get('arguments', {}))
        return parsed

    # 兜底：原文里夹了自然语言前缀，抽出第一个平衡的 {...} 再解析
    sub = _extract_tool_call_substring(raw)
    if sub:
        parsed = _try_parse_json_or_literal(sub)
        if isinstance(parsed, dict) and 'name' in parsed:
            parsed['arguments'] = _coerce_arguments(parsed.get('arguments', {}))
            return parsed
    return None


def _try_parse_json_or_literal(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    try:
        return ast.literal_eval(text)
    except Exception:
        return None


class Callback(OmniRealtimeCallback):
    def __init__(self, session: Session):
        self.session = session
        self.full_response = ""
        self.sentence_buffer = ""
        self.speech_start_time = None
        self.response_id = f"resp_{int(time.time() * 1000)}"
        self.pending_tool_call = None
        self.is_tool_call_mode = False
        self.response_active = False

        # 标点切片：累积流式文本，遇到任意标点就切一段送出。
        # 不再设字符数上限/下限——若整段没标点，会等到响应结束 force_all 一次性发。
        # 强结束符 + 弱分隔符都视为切点。
        self.STRONG_ENDINGS = set('。.！？!?\n')
        self.WEAK_SPLITTERS = set(',，“”<>;；:：')
        self.SPLIT_CHARS = self.STRONG_ENDINGS | self.WEAK_SPLITTERS

        
    def on_open(self) -> None:
        logger.info(f"[{self.session.session_id}] DashScope 已连接")
        
    def on_close(self, close_status_code, close_msg) -> None:
        logger.info(f"[{self.session.session_id}] DashScope 断开 | Code: {close_status_code} | Reason: {close_msg}")
        
        if close_status_code == 1000:
            logger.info(f"[{self.session.session_id}] 正常关闭 (Normal Closure)")
        elif close_status_code == 1001:
            logger.warning(f"[{self.session.session_id}] 服务端正在关闭/离开 (Going Away)")
        elif close_status_code == 1002:
            logger.error(f"[{self.session.session_id}] 协议错误 (Protocol Error)")
        elif close_status_code == 1008:
            logger.warning(f"[{self.session.session_id}] 策略违规/无效数据 (Policy Violation)")
        elif close_status_code == 1011:
            logger.error(f"[{self.session.session_id}] 服务端内部错误 (Internal Error)")
        elif close_status_code is None:
            logger.error(f"[{self.session.session_id}] 异常断开 (Abnormal Closure) - 可能是网络中断或连接超时")
        else:
            logger.warning(f"[{self.session.session_id}] 未知断开代码: {close_status_code}")

        self.session.reconnect_needed = True
        
    def _flush_buffer(self, is_final=False, force_all=False):
        """只用标点切。
        - force_all=True：一次性把缓冲区全部发出（响应结束时用）
        - 否则：扫第一个标点（强或弱）就切并发出；没有标点则不发，继续累积
        """
        if not self.sentence_buffer:
            return

        if force_all:
            text_to_send = self.sentence_buffer
            self.sentence_buffer = ""
        else:
            split_pos = -1
            for i, ch in enumerate(self.sentence_buffer):
                if ch in self.SPLIT_CHARS:
                    split_pos = i + 1  # 包含标点
                    break
            if split_pos == -1:
                return
            text_to_send = self.sentence_buffer[:split_pos]
            self.sentence_buffer = self.sentence_buffer[split_pos:]

        data = {
            "type": "response",
            "response_id": self.response_id,
            "text_delta": text_to_send,
            "audio_delta": None,
            "is_final": is_final,
            "tool_call": {},
            "timestamp": int(time.time() * 1000)
        }

        self._send(data)

    def on_event(self, response: dict) -> None:
        try:
            event_type = response.get('type')
            logger.debug(f"[{self.session.session_id}] DashScope 事件：{event_type}")
            
            if event_type == 'session.created':
                logger.info(f"[{self.session.session_id}] 会话创建")
                
            elif event_type == 'conversation.item.input_audio_transcription.completed':
                transcript = response.get('transcript', '')
                logger.info(f"[{self.session.session_id}] 🗣️ 识别：{transcript}")
                self.session.log_user(transcript)
                self._send({
                    "type": "transcription",
                    "text": transcript,
                    "timestamp": int(time.time() * 1000)
                })
                # 硬关键词命中即直接发 tool_call；命中后本轮 LLM 增量全丢
                try_fire_keyword_tool(self.session, transcript)
                
            elif event_type == 'response.audio_transcript.delta':
                text = response.get('delta', '')
                if not text:
                    return
                # 硬关键词已经在 ASR 句末发过 tool_call，本轮 LLM 增量全部丢弃
                if is_round_handled(self.session):
                    return

                logger.debug(f"[{self.session.session_id}] 📤 原始文本流：{text}")

                if not self.response_active:
                    self.response_active = True
                    if self.speech_start_time:
                        delay = (time.time() - self.speech_start_time) * 1000
                        provider = (self.session.config or {}).get('provider', 'dashscope')
                        logger.info(
                            f"[latency] session={self.session.session_id} provider={provider} "
                            f"round={self.session.round} ttft={int(delay)}ms"
                        )
                        self.speech_start_time = None

                # 累加完整响应（用于工具检测等）
                self.full_response += text

                if not self.is_tool_call_mode:
                    stripped_resp = self.full_response.lstrip()
                    if _looks_like_tool_call_start(stripped_resp):
                        self.is_tool_call_mode = True
                        logger.info(f"[{self.session.session_id}] 🔧 检测到工具调用模式 (起始特征: {stripped_resp[:20]})")
                        # 清空已缓冲的文本，避免转发工具调用的开头部分
                        self.sentence_buffer = ""
                        return

                # 如果处于工具调用模式，不转发流式文本
                if self.is_tool_call_mode:
                    return

                self.sentence_buffer += text
                # 只要 buffer 里还有标点就一直切；没有就停下继续累积
                while any(ch in self.SPLIT_CHARS for ch in self.sentence_buffer):
                    prev_len = len(self.sentence_buffer)
                    self._flush_buffer(is_final=False)
                    if len(self.sentence_buffer) == prev_len:
                        break

            elif event_type == 'response.text.delta':
                text = response.get('delta', '')
                if not text:
                    return
                if is_round_handled(self.session):
                    return

                logger.debug(f"[{self.session.session_id}] 📤 原始文本流 (text.delta)：{text}")

                if not self.response_active:
                    self.response_active = True
                    if self.speech_start_time:
                        delay = (time.time() - self.speech_start_time) * 1000
                        provider = (self.session.config or {}).get('provider', 'dashscope')
                        logger.info(
                            f"[latency] session={self.session.session_id} provider={provider} "
                            f"round={self.session.round} ttft={int(delay)}ms"
                        )
                        self.speech_start_time = None

                self.full_response += text

                if not self.is_tool_call_mode:
                    stripped_resp = self.full_response.lstrip()
                    if _looks_like_tool_call_start(stripped_resp):
                        self.is_tool_call_mode = True
                        logger.info(f"[{self.session.session_id}] 🔧 检测到工具调用模式 (text.delta)")
                        # 清空已缓冲的文本，避免转发工具调用的开头部分
                        self.sentence_buffer = ""
                        return

                if self.is_tool_call_mode:
                    return

                # 同上：标点切片
                self.sentence_buffer += text
                while any(ch in self.SPLIT_CHARS for ch in self.sentence_buffer):
                    prev_len = len(self.sentence_buffer)
                    self._flush_buffer(is_final=False)
                    if len(self.sentence_buffer) == prev_len:
                        break
            
            elif event_type == 'response.done':
                logger.info(f"[{self.session.session_id}] ✅ 响应完成, 📝 完整回复: {self.full_response}")

                # 本轮已被硬关键词路由处理：路由那边已经 send 过 is_final + 递增过
                # chat_index，这里只清状态，不再重发也不再递增
                if is_round_handled(self.session):
                    logger.info(
                        f"[{self.session.session_id}] 🔑 本轮已由关键词路由处理，"
                        f"丢弃 LLM 完整回复"
                    )
                    self.full_response = ""
                    self.sentence_buffer = ""
                    self.is_tool_call_mode = False
                    self.response_active = False
                    self.speech_start_time = None
                    self.response_id = f"resp_{int(time.time() * 1000)}"
                    return

                if self.is_tool_call_mode:
                    # 工具调用模式下的处理逻辑保持不变
                    tool_call_data = None
                    raw_content = self.full_response.strip()

                    try:
                        tool_call_data = _parse_tool_call_text(raw_content)
                        if tool_call_data is None:
                            raise ValueError("无法解析为合法的工具调用结构")

                        logger.info(f"[{self.session.session_id}] 🔧 成功解析工具调用: {tool_call_data['name']}")
                        self.session.log_ai(f"[工具调用] {json.dumps(tool_call_data, ensure_ascii=False)}")

                        # image_editing.image_to_edit / image_to_3d.src_image 必须是
                        # 已记录在 image_tracker 列表里的真实 ID；缺失则回退一句话。
                        new_tcd, img_err = self.session.image_tracker.apply_to_tool_call(tool_call_data)
                        if img_err:
                            self._send({
                                "type": "response",
                                "response_id": self.response_id,
                                "text_delta": img_err,
                                "audio_delta": None,
                                "is_final": True,
                                "tool_call": {},
                                "timestamp": int(time.time() * 1000)
                            })
                        else:
                            self._send({
                                "type": "response",
                                "response_id": self.response_id,
                                "text_delta": "",
                                "audio_delta": None,
                                "is_final": True,
                                "tool_call": new_tcd,
                                "timestamp": int(time.time() * 1000)
                            })

                    except Exception as e:
                        logger.error(f"[{self.session.session_id}] ❌ 工具调用解析/校验失败: {e}")
                        logger.error(f"[{self.session.session_id}] 原始内容: {raw_content}")
                        
                        self.session.log_ai(raw_content)
                        self._send({
                            "type": "response",
                            "response_id": self.response_id,
                            "text_delta": raw_content, 
                            "audio_delta": None,
                            "is_final": True,
                            "tool_call": {},
                            "timestamp": int(time.time() * 1000)
                        })
                else:
                    # 普通文本模式
                    # 1. 强制发送缓冲区中所有剩余文本
                    if self.sentence_buffer:
                        self._flush_buffer(is_final=False, force_all=True)
                    
                    # 2. 发送最终的 is_final=True 标记
                    self.session.log_ai(self.full_response)
                    self._send({
                        "type": "response",
                        "response_id": self.response_id,
                        "text_delta": "",
                        "audio_delta": None,
                        "is_final": True,
                        "tool_call": {},
                        "timestamp": int(time.time() * 1000)
                    })
                
                # 重置所有状态
                self.full_response = ""
                self.sentence_buffer = ""
                self.is_tool_call_mode = False
                self.response_active = False
                self.session.chat_index += 1
                self.speech_start_time = None
                self.response_id = f"resp_{int(time.time() * 1000)}"
                
            elif event_type == 'input_audio_buffer.speech_started':
                logger.info(f"[{self.session.session_id}] 🎤 VAD 检测到语音")
                self.speech_start_time = time.time()
                self._send({
                    "type": "vad_started",
                    "timestamp": int(time.time() * 1000)
                })
            
            elif event_type == 'input_audio_buffer.speech_stopped':
                logger.info(f"[{self.session.session_id}] 🎤 VAD 语音结束")
                self._send({
                    "type": "vad_stopped",
                    "timestamp": int(time.time() * 1000)
                })
                
            elif event_type == 'error':
                error_info = response.get('error', {})
                error_code = error_info.get('code')
                logger.error(f"[{self.session.session_id}] ❌ 错误：{response}")
                self._send({
                    "type": "error",
                    "message": error_info.get('message', 'Unknown'),
                    "timestamp": int(time.time() * 1000)
                })
                if error_code == 'response_idle_timeout':
                    logger.info(f"[{self.session.session_id}] 检测到idle timeout，准备重连")
                    self.session.reconnect_needed = True
                    self._schedule_reconnect()
                if self.response_active:
                    self.response_active = False
                    self.session.chat_index += 1
                
        except Exception as e:
            logger.error(f"[{self.session.session_id}] 回调错误：{e}")

    def _schedule_reconnect(self):
        """主动触发重连，不依赖下次 media.stream 到达。"""
        loop = getattr(self.session, 'websocket_loop', None)
        if not loop:
            return
        session = self.session

        async def _do():
            from ..server.websocket import _reconnect_provider
            session.reconnect_needed = False
            await _reconnect_provider(session, "proactive after idle_timeout")

        asyncio.run_coroutine_threadsafe(_do(), loop)

    def _send(self, data: dict):
        data['chat_index'] = self.session.chat_index 
        if self.session.websocket and not self.session.is_closed:
            try:
                loop = getattr(self.session, 'websocket_loop', None)
                if loop is None:
                    loop = get_event_loop()
                asyncio.run_coroutine_threadsafe(
                    self.session.websocket.send(json.dumps(data, ensure_ascii=False)),
                    loop
                )
                if data.get('type') == 'response':
                    is_final = data.get('is_final')
                    tool_call = data.get('tool_call', {})
                    if tool_call:
                        logger.info(f"[{self.session.session_id}] ✅ 已转发工具调用 (chat_index={self.session.chat_index}, is_final={is_final})")
                    else:
                        text_delta = data.get('text_delta', '')
                        # 【修改】日志显示完整的发送文本（不再截断），方便调试
                        logger.info(f"[{self.session.session_id}] ✅ 已转发文本: {text_delta} (chat_index={self.session.chat_index}, is_final={is_final})")
            except Exception as e:
                logger.error(f"[{self.session.session_id}] 发送错误：{e}")