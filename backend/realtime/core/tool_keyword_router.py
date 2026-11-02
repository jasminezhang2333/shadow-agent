"""
硬关键词触发器：在 LLM 之前用 ASR 文本直接命中工具调用。

设计动机
--------
config.py 的 INSTRUCTIONS 里有"意图识别"段落，依赖模型读懂关键词后再决定要不要
调工具。实测对意图理解能力较弱的模型不稳定。本模块在 ASR 句末文本上做硬性
substring 匹配，命中即直接通过 WebSocket 下发一个 tool_call 响应；同时设置
session._kw_tool_fired_round 标志，让回调把同一轮的 LLM 文本/工具输出全部丢弃，
避免出现"工具调用 + 重复的文本回复"双发。

优先级（自上而下，先命中即停止）
1. image_to_3d        —— 3D / 三维 / 模型 / 建模 / OBJ ...
2. comparison_prices  —— 价格 / 多少钱 / 比价 ...
3. memory_retrieval   —— 记得 / 回忆 / 忘 / 在哪 ...

负向词
- image_to_3d 排除"3D 风格图片 / 3D 渲染图"这种描述图片风格而非模型的说法
- comparison_prices 排除"这是什么牌子 / 什么型号 / 哪一款 / 画面里是什么"
  这种纯识别问询（即使句中出现产品词或"买"，也不该走比价）

调用方约定
- 在每个 callback 的 ASR 句末（input_audio_transcription.completed 之类）调
  session.log_user(text) 之后，立即调用 try_fire_keyword_tool(session, text)。
- 命中返回 True 时，回调里的文本/音频/工具增量都要丢弃，response.done 不再
  发 is_final 也不要再递增 chat_index（路由已经替它发过了）。
- 用 is_round_handled(session) 比对 session.round 来判断"本轮是否已被硬触发处理"。
"""
import json
import os
import time
import asyncio
from typing import Optional, List, Tuple

from ..config import Settings
from .session import Session, get_event_loop
from ..utils.logger import logger


# 规则文件路径：与本模块同目录 tool_keywords.json。运行时加载一次，缺失/解析失败
# 时回退到 _DEFAULT_RULES。改 JSON 后需要重启服务。
_RULES_JSON_PATH = os.path.join(os.path.dirname(__file__), 'tool_keywords.json')


# (tool_name, positives, negatives) —— 顺序即优先级
# JSON 文件缺失时的兜底；正常运行从 JSON 读取。
# 注意：image_generating / image_editing 的 user_query 必须是英文，纯关键词
# 路由没法翻译，所以这两个工具不在路由里——交给 LLM 按 INSTRUCTIONS 处理。
_DEFAULT_RULES: List[Tuple[str, List[str], List[str]]] = [
    (
        'image_to_3d',
        [
            '3D打印', '3D模型', '三维模型', '可旋转模型', '立体模型',
            '建模', '转模型', '生成模型', '做成模型', '做个模型',
            '转成3D', '做成3D', '生成3D', '三维打印',
            'OBJ', 'GLB', 'STL', 'USDZ',
            '3D', '三维', '模型',
        ],
        ['3D风格图片', '3D效果图', '3D渲染图', '立体风格海报', '3D海报'],
    ),
    (
        'comparison_prices',
        [
            '价格', '多少钱', '几块', '便宜', '贵', '钱',
            '报价', '比价', '对比价格',
            '其他品牌', '哪里买', '划算', '购买', '下单', '买',
            '淘宝', '京东', '拼多多', '天猫', '京造',
            '官网价', '旗舰店', '性价比',
        ],
        [
            '这是什么', '这是哪款', '什么牌子', '什么型号', '什么品牌',
            '哪一款', '哪款手机', '哪款电脑', '识别一下',
            '画面里是什么', '看看这是什么', '这是啥',
        ],
    ),
    (
        'memory_retrieval',
        [
            '记得', '回忆', '忘', '在哪', '放哪', '找不到',
            '刚才看到', '之前看到', '我看过', '记',
        ],
        [],
    ),
]


def _load_rules() -> List[Tuple[str, List[str], List[str]]]:
    """从 JSON 文件加载关键词规则；任何异常都回退到 _DEFAULT_RULES。"""
    try:
        with open(_RULES_JSON_PATH, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        raw_rules = payload.get('rules')
        if not isinstance(raw_rules, list) or not raw_rules:
            raise ValueError("rules 字段缺失或非数组")
        parsed: List[Tuple[str, List[str], List[str]]] = []
        for idx, item in enumerate(raw_rules):
            if not isinstance(item, dict):
                raise ValueError(f"rules[{idx}] 不是 object")
            tool = item.get('tool')
            positives = item.get('positives') or []
            negatives = item.get('negatives') or []
            if not isinstance(tool, str) or not tool:
                raise ValueError(f"rules[{idx}].tool 非法")
            if not isinstance(positives, list) or not all(isinstance(s, str) for s in positives):
                raise ValueError(f"rules[{idx}].positives 必须是字符串数组")
            if not isinstance(negatives, list) or not all(isinstance(s, str) for s in negatives):
                raise ValueError(f"rules[{idx}].negatives 必须是字符串数组")
            parsed.append((tool, list(positives), list(negatives)))
        logger.info(
            f"[tool_keyword_router] 已从 {_RULES_JSON_PATH} 加载 {len(parsed)} 条规则"
        )
        return parsed
    except FileNotFoundError:
        logger.warning(
            f"[tool_keyword_router] 未找到 {_RULES_JSON_PATH}，回退到内置规则"
        )
        return _DEFAULT_RULES
    except Exception as e:
        logger.error(
            f"[tool_keyword_router] 加载 {_RULES_JSON_PATH} 失败({type(e).__name__}: {e})，"
            f"回退到内置规则"
        )
        return _DEFAULT_RULES


_RULES: List[Tuple[str, List[str], List[str]]] = _load_rules()


def _match_rule(text: str) -> Optional[str]:
    for tool_name, positives, negatives in _RULES:
        if any(neg in text for neg in negatives):
            continue
        if any(kw in text for kw in positives):
            return tool_name
    return None


def _build_arguments(tool_name: str, transcript: str, current_image_id: Optional[str]):
    """构造 arguments；image_to_3d 缺 image_id 时返回 (None, fallback_text)。

    注意：image_generating / image_editing 不在路由里（user_query 要求英文，
    路由没法翻译），所以这里不处理它们。
    """
    if tool_name == 'memory_retrieval':
        return {'query': transcript}, None
    if tool_name == 'comparison_prices':
        return {'user_input': transcript}, None
    if tool_name == 'image_to_3d':
        if not current_image_id:
            return None, '请先提供一张图片，或先让我生成一张参考图。'
        return {'src_image': current_image_id}, None
    return None, None


def _send(session: Session, data: dict):
    data['chat_index'] = session.chat_index
    if not session.websocket or session.is_closed:
        return
    try:
        loop = getattr(session, 'websocket_loop', None) or get_event_loop()
        asyncio.run_coroutine_threadsafe(
            session.websocket.send(json.dumps(data, ensure_ascii=False)),
            loop,
        )
    except Exception as e:
        logger.error(f"[{session.session_id}] 关键词路由发送错误：{e}")


def try_fire_keyword_tool(session: Session, transcript: str) -> bool:
    """ASR 句末文本命中关键词时直接发工具调用。

    命中返回 True，并把 session._kw_tool_fired_round 设为 session.round；
    回调里随后到达的 LLM 文本/工具增量应以 is_round_handled(session) 为准丢弃。
    """
    if not getattr(Settings, 'KEYWORD_ROUTER_ENABLED', True):
        return False
    if not transcript:
        return False
    text = transcript.strip()
    if not text:
        return False
    tool_name = _match_rule(text)
    if not tool_name:
        return False

    current_image_id = session.image_tracker.current
    args, fallback = _build_arguments(tool_name, text, current_image_id)
    response_id = f"resp_{int(time.time() * 1000)}"

    if fallback is not None:
        logger.info(
            f"[{session.session_id}] 🔑 关键词命中 {tool_name} 但无 image_id，"
            f"回退一句并抑制 LLM：{fallback!r}"
        )
        try:
            session.log_ai(fallback)
        except Exception:
            pass
        _send(session, {
            'type': 'response',
            'response_id': response_id,
            'text_delta': fallback,
            'audio_delta': None,
            'is_final': True,
            'tool_call': {},
            'timestamp': int(time.time() * 1000),
        })
        session._kw_tool_fired_round = session.round
        session.chat_index += 1
        return True

    tool_call_data = {
        'name': tool_name,
        'arguments': args,
    }
    logger.info(
        f"[{session.session_id}] 🔑 关键词路由命中 {tool_name}: args={args} "
        f"transcript={text!r}"
    )
    try:
        session.log_ai(f"[工具调用] {json.dumps(tool_call_data, ensure_ascii=False)}")
    except Exception:
        pass
    _send(session, {
        'type': 'response',
        'response_id': response_id,
        'text_delta': '',
        'audio_delta': None,
        'is_final': True,
        'tool_call': tool_call_data,
        'timestamp': int(time.time() * 1000),
    })
    session._kw_tool_fired_round = session.round
    session.chat_index += 1
    return True


def is_round_handled(session: Session) -> bool:
    """本轮（session.round）是否已经被硬关键词触发处理过。"""
    return getattr(session, '_kw_tool_fired_round', None) == session.round
