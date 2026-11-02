"""任务 agent：自然语言任务 → 看虚拟屏 → 决策 → phone_act → 验证 的闭环。

这不是回放脚本：每一步都基于「当前虚拟屏实际状态」由 LLM 决定下一动作，
能应对弹窗、加载、页面变化。遇到支付/登录/验证码 → 产出 HANDOFF，停下来
交给用户（见 docs/DESIGN.md §4）。

决策 LLM 的输入 = 任务目标 + 当前屏 UI dump（文本化）+ 已执行历史。
输出 = 一个 JSON 动作。无 LLM key 时退化为「只观察并报告」，保证可测。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from ..tools.phone_act import Action, PhoneActuator


@dataclass
class TaskState:
    goal: str
    steps: list[dict] = field(default_factory=list)
    done: bool = False
    handoff: str = ""           # 非空 = 需要用户介入，内容是交接说明
    result: str = ""

    def record(self, action: Action, obs_summary: str) -> None:
        self.steps.append({
            "action": action.type,
            "detail": {k: v for k, v in action.__dict__.items()
                       if v not in ("", 0)},
            "obs": obs_summary,
        })


_HANDOFF_SIGNALS = ["支付", "付款", "收银台", "登录", "验证码", "指纹", "人脸",
                    "密码", "实名"]

_DECISION_PROMPT = """你在一块用户看不见的虚拟屏上操作 App，替用户完成任务。
任务目标：{goal}

当前屏幕 UI（文本化层级，含可点击控件的 text/bounds）：
{ui}

已执行步骤：{history}

规则：
- 每次只输出一个 JSON 动作，不要解释。
- 动作格式：{{"type":"tap","x":int,"y":int}} 或 swipe/text/key/launch/back。
- 若目标已达成，输出 {{"type":"done","result":"..."}}。
- 若需要用户本人完成（支付/登录/验证码），输出
  {{"type":"handoff","reason":"已选好XX，停在支付页，待你确认"}}。
下一步动作 JSON："""


class TaskAgent:
    def __init__(self, actuator: PhoneActuator, max_steps: int = 25):
        self.actuator = actuator
        self.max_steps = max_steps
        self._llm = self._maybe_llm()

    @staticmethod
    def _maybe_llm():
        key = os.getenv("DASHSCOPE_API_KEY", "")
        if not key:
            return None
        try:
            from langchain_community.chat_models.tongyi import ChatTongyi

            return ChatTongyi(model="qwen-plus", dashscope_api_key=key)
        except Exception:
            return None

    @staticmethod
    def _summarize_ui(ui_xml: str, limit: int = 4000) -> str:
        # 真实工程里会解析 uiautomator XML 抽 clickable 节点；这里做长度裁剪兜底
        return ui_xml[:limit] if ui_xml else "(无 UI dump，仅截图可用)"

    def _decide(self, state: TaskState, ui_summary: str) -> Action:
        if self._llm is None:
            # 退化模式：不瞎点，直接请求交接，避免误操作用户账号
            raise _DegradedStop()
        prompt = _DECISION_PROMPT.format(
            goal=state.goal, ui=ui_summary,
            history=json.dumps(state.steps, ensure_ascii=False)[:2000])
        resp = self._llm.invoke(prompt)
        raw = getattr(resp, "content", str(resp)).strip()
        raw = raw.strip("`").replace("json\n", "", 1)
        obj = json.loads(raw)
        return self._to_action(obj, state)

    @staticmethod
    def _to_action(obj: dict, state: TaskState) -> Action:
        t = obj.get("type")
        if t == "done":
            state.done = True
            state.result = obj.get("result", "")
            return Action(type="key", keycode=0)  # no-op
        if t == "handoff":
            state.handoff = obj.get("reason", "需要用户介入")
            return Action(type="key", keycode=0)
        return Action(
            type=t, x=obj.get("x", 0), y=obj.get("y", 0),
            x2=obj.get("x2", 0), y2=obj.get("y2", 0),
            text=obj.get("text", ""), keycode=obj.get("keycode", 0),
            package=obj.get("package", ""), activity=obj.get("activity", ""))

    def run(self, goal: str, verify=None) -> TaskState:
        """verify: Optional[Callable[[str ui_xml], bool]] 完成断言。"""
        state = TaskState(goal=goal)
        for _ in range(self.max_steps):
            obs = self.actuator.observe()
            ui_summary = self._summarize_ui(obs.get("ui_xml", ""))

            # 自动检测交接信号
            if any(sig in ui_summary for sig in _HANDOFF_SIGNALS):
                state.handoff = ("检测到支付/登录/验证页面，已停下等待用户本人确认。")
                break
            try:
                action = self._decide(state, ui_summary)
            except _DegradedStop:
                state.handoff = ("未配置决策 LLM，为避免误操作用户账号，"
                                 "已停在当前页等待人工接管。")
                break

            if state.done or state.handoff:
                break
            self.actuator.act(action)
            state.record(action, ui_summary[:200])

            if verify is not None:
                obs2 = self.actuator.observe()
                if verify(obs2.get("ui_xml", "")):
                    state.done = True
                    state.result = "完成断言通过"
                    break
        if not state.done and not state.handoff:
            state.handoff = "达到最大步数仍未完成，交人工检查。"
        return state


class _DegradedStop(Exception):
    pass
