"""
ImageIdTracker：会话级别追踪图片生成/编辑工具调用产出的图片 ID。

数据来源：上游 Agent 在工具执行完成后会通过 /v1/realtime/text 把 function 结果
（{"role":"function","name":"image_editing","content":{"result":"edit_mock"}}）
回流给本服务。HTTP 入口在收到这种 payload 时调用 record_function_result，
把 result 里的图片 ID 追加进列表。

使用约束（来自 prompt 规则 #8/#9/#10/#11/#13/#14/#15/#16）：
- image_editing 的 image_to_edit、image_to_3d 的 src_image，传值必须在已记录的列表里。
- 没有合法图片 ID 时，外面应回退一句"请先提供要编辑的图片"。
- 严禁占位符（{{current_media}}, current_media, {{image}}, image id 等）。
"""
import json
import threading
from typing import List, Optional, Tuple

from ..utils.logger import logger


# 占位符黑名单：模型可能输出这些字符串，必须拒绝
_PLACEHOLDER_BLACKLIST = {
    '{{current_media}}', 'current_media',
    '{{image}}', 'image', 'image id', 'image_id',
    '最近图片', '上一张图', '参考图', '当前图片',
    '', None,
}

# 产生图片 ID 的工具
_IMAGE_PRODUCING_TOOLS = {'image_generating', 'image_editing'}


class ImageIdTracker:
    def __init__(self, session_id: str = ''):
        self._session_id = session_id
        self._lock = threading.Lock()
        self._image_ids: List[str] = []  # 按时间顺序，去重

    @property
    def current(self) -> Optional[str]:
        with self._lock:
            return self._image_ids[-1] if self._image_ids else None

    def list_all(self) -> List[str]:
        with self._lock:
            return list(self._image_ids)

    def is_valid(self, image_id) -> bool:
        if not isinstance(image_id, str):
            return False
        if image_id in _PLACEHOLDER_BLACKLIST:
            return False
        with self._lock:
            return image_id in self._image_ids

    def _add(self, image_id: str):
        if not isinstance(image_id, str) or not image_id:
            return
        if image_id in _PLACEHOLDER_BLACKLIST:
            return
        with self._lock:
            if image_id in self._image_ids:
                # 把它移到末尾（视为最新引用）
                self._image_ids.remove(image_id)
            self._image_ids.append(image_id)
        logger.info(
            f"[{self._session_id}] 🖼️  ImageIdTracker add: {image_id} "
            f"(now: {self._image_ids})"
        )

    def record_function_result(self, name: str, content) -> Optional[str]:
        """从 function result 里抽 image_id 加进列表，返回抽到的 id（没抽到 None）。

        content 形态可能是：
        - dict: {"result": "edit_mock"}
        - str: '{"result":"edit_mock"}' 或裸字符串 'edit_mock'
        """
        if name not in _IMAGE_PRODUCING_TOOLS:
            return None
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except Exception:
                parsed = {'result': content}
        else:
            parsed = content
        if not isinstance(parsed, dict):
            return None
        image_id = parsed.get('result')
        if not isinstance(image_id, str) or not image_id:
            return None
        self._add(image_id)
        return image_id

    def enforce(self, tool_name: str, arguments: dict) -> Tuple[dict, Optional[str]]:
        """检查/修正 image_editing 的 image_to_edit、image_to_3d 的 src_image。

        策略：
        - 如果用户给的值已经在 image_ids 列表里 → 保留原值
        - 如果列表非空但用户给的值是占位符/不在列表 → 替换成 current（最近一个）
        - 如果列表为空 → 返回 (arguments, error_message) 让上层回退一句话

        返回：(arguments_dict, error_message_or_None)
        """
        if not isinstance(arguments, dict):
            return arguments, None

        if tool_name == 'image_editing':
            field = 'image_to_edit'
        elif tool_name == 'image_to_3d':
            field = 'src_image'
        else:
            return arguments, None

        with self._lock:
            valid_ids = list(self._image_ids)
            current = valid_ids[-1] if valid_ids else None

        provided = arguments.get(field)

        if isinstance(provided, str) and provided in valid_ids:
            return arguments, None

        if current is None:
            # 列表为空：上层应该回退一句"请先提供要编辑的图片 / 参考图"
            err = (
                "请先提供要编辑的图片，或先让我生成一张参考图。"
                if tool_name == 'image_editing'
                else "请先提供一张图片，或先让我生成一张参考图。"
            )
            logger.warning(
                f"[{self._session_id}] ⚠️ {tool_name} 缺少合法图片ID；"
                f"原 {field}={provided!r}，列表为空"
            )
            return arguments, err

        # 列表非空但提供值不合法 → 强制替换成 current
        new_args = dict(arguments)
        new_args[field] = current
        logger.info(
            f"[{self._session_id}] 🔧 {tool_name} 修正 {field}: {provided!r} → {current!r} "
            f"(候选: {valid_ids})"
        )
        return new_args, None

    def apply_to_tool_call(self, tool_call_data):
        """对一个完整的 tool_call_data（{name, arguments, ...}）做 enforce。

        返回 (new_tool_call_data | None, err_text | None)：
        - 没问题：返回 (修正后的 tool_call_data, None)
        - 缺合法图片 ID：返回 (None, "请先提供要编辑的图片...")，调用方应把这一条
          tool_call 替换成普通文本回复。
        """
        if not isinstance(tool_call_data, dict):
            return tool_call_data, None
        name = tool_call_data.get('name')
        if name not in ('image_editing', 'image_to_3d'):
            return tool_call_data, None
        args = tool_call_data.get('arguments') or {}
        new_args, err = self.enforce(name, args)
        if err:
            return None, err
        new_tcd = dict(tool_call_data)
        new_tcd['arguments'] = new_args
        return new_tcd, None
