"""phone_act 工具：agent 对虚拟屏 App 的最小动作集。

agent 的决策循环产出一个 action（点哪、滑哪、输什么），phone_act 翻译成
AdbBridge 调用，全部定向到虚拟 display N，绝不碰用户物理屏 display 0。

动作集刻意收敛到可组合的原子操作，便于 agent 规划，也便于断言验证。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..bridge.adb_bridge import AdbBridge

ActionType = Literal["tap", "swipe", "text", "key", "launch", "back", "home"]

# 常用 keycode
KEYCODE_BACK = 4
KEYCODE_HOME = 3
KEYCODE_ENTER = 66


@dataclass
class Action:
    type: ActionType
    x: int = 0
    y: int = 0
    x2: int = 0
    y2: int = 0
    text: str = ""
    keycode: int = 0
    package: str = ""
    activity: str = ""


class PhoneActuator:
    def __init__(self, bridge: AdbBridge, display_id: int):
        self.bridge = bridge
        self.display_id = display_id

    def act(self, action: Action) -> dict:
        d = self.display_id
        if action.type == "tap":
            self.bridge.tap(action.x, action.y, d)
        elif action.type == "swipe":
            self.bridge.swipe(action.x, action.y, action.x2, action.y2, d)
        elif action.type == "text":
            self.bridge.input_text(action.text, d)
        elif action.type == "key":
            self.bridge.keyevent(action.keycode, d)
        elif action.type == "launch":
            self.bridge.start_app(action.package, d, action.activity or None)
        elif action.type == "back":
            self.bridge.keyevent(KEYCODE_BACK, d)
        elif action.type == "home":
            self.bridge.keyevent(KEYCODE_HOME, d)
        else:
            return {"ok": False, "error": f"unknown action {action.type}"}
        return {"ok": True, "action": action.type}

    def observe(self) -> dict:
        """返回虚拟屏当前状态：截帧 + UI 层级 dump，供 agent 决策下一步。"""
        png = self.bridge.screencap(self.display_id)
        ui_xml = ""
        try:
            ui_xml = self.bridge.uidump(self.display_id)
        except Exception:
            pass  # 某些 ROM 多屏 uidump 不支持，退化为只给截图
        return {"screenshot_png": png, "ui_xml": ui_xml}
