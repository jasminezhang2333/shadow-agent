"""ADB 分身桥接层：在不可见的虚拟 display 上管理 App 并注入输入。

设计要点（见 docs/DESIGN.md §0、§6）：
- 虚拟屏用 scrcpy 3.x `--new-display` 在非 root 设备创建，拿到 display id。
- App 用 `am start --display N` 启到虚拟屏；用户物理屏是 display 0，互不干扰。
- 输入注入用 `input --display N tap/swipe/text`（Android 10+ 支持 --display）。
- 虚拟屏截帧用 `screencap -d N`。

命令构造（_build_* 方法）与命令执行（_run）分离：前者纯函数、可在任意平台单测，
后者才真正调用 adb，需要连上真机。这样 Windows 上也能验证命令拼装正确性。
"""
from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass


@dataclass
class VirtualDisplay:
    display_id: int
    width: int
    height: int
    density: int


class AdbBridge:
    def __init__(self, serial: str | None = None, adb: str = "adb"):
        self.serial = serial
        self.adb = adb

    # ---------- 命令构造（纯函数，可单测） ----------

    def _base(self) -> list[str]:
        cmd = [self.adb]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def _build_shell(self, *args: str) -> list[str]:
        return self._base() + ["shell", *args]

    def _build_start_app(self, package: str, display_id: int,
                         activity: str | None = None) -> list[str]:
        component = f"{package}/{activity}" if activity else None
        args = ["am", "start", "--display", str(display_id)]
        if component:
            args += ["-n", component]
        else:
            # 用 monkey 兜底拉起默认 launcher activity 的写法交给调用方；
            # 这里走标准 am start -a MAIN -c LAUNCHER
            args = ["am", "start", "--display", str(display_id),
                    "-a", "android.intent.action.MAIN",
                    "-c", "android.intent.category.LAUNCHER",
                    package]
        return self._build_shell(*args)

    def _build_tap(self, x: int, y: int, display_id: int) -> list[str]:
        return self._build_shell("input", "-d", str(display_id), "tap",
                                 str(x), str(y))

    def _build_swipe(self, x1: int, y1: int, x2: int, y2: int,
                     display_id: int, duration_ms: int = 300) -> list[str]:
        return self._build_shell("input", "-d", str(display_id), "swipe",
                                 str(x1), str(y1), str(x2), str(y2),
                                 str(duration_ms))

    def _build_text(self, text: str, display_id: int) -> list[str]:
        # input text 不能直接带空格，空格转义为 %s；复杂中文建议走 ADBKeyboard broadcast
        safe = text.replace(" ", "%s")
        return self._build_shell("input", "-d", str(display_id), "text",
                                 shlex.quote(safe))

    def _build_keyevent(self, keycode: int, display_id: int) -> list[str]:
        return self._build_shell("input", "-d", str(display_id), "keyevent",
                                 str(keycode))

    def _build_screencap(self, display_id: int) -> list[str]:
        # -p 输出 PNG 到 stdout；-d 指定 display
        return self._build_shell("screencap", "-d", str(display_id), "-p")

    def _build_uidump(self, display_id: int) -> list[str]:
        # uiautomator dump 默认 display 0；多屏需 --display（部分 ROM 支持）
        return self._build_shell("uiautomator", "dump", "--display",
                                 str(display_id), "/sdcard/ui_dump.xml")

    # ---------- 命令执行（需要真机） ----------

    def _run(self, cmd: list[str], binary: bool = False) -> bytes | str:
        out = subprocess.run(cmd, capture_output=True,
                             check=False, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(
                f"adb cmd failed ({out.returncode}): {' '.join(cmd)}\n"
                f"{out.stderr.decode('utf-8', 'ignore')}")
        return out.stdout if binary else out.stdout.decode("utf-8", "ignore")

    # ---------- 高层 API ----------

    def start_app(self, package: str, display_id: int,
                  activity: str | None = None) -> str:
        return self._run(self._build_start_app(package, display_id, activity))

    def tap(self, x: int, y: int, display_id: int) -> str:
        return self._run(self._build_tap(x, y, display_id))

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              display_id: int, duration_ms: int = 300) -> str:
        return self._run(self._build_swipe(x1, y1, x2, y2,
                                           display_id, duration_ms))

    def input_text(self, text: str, display_id: int) -> str:
        return self._run(self._build_text(text, display_id))

    def keyevent(self, keycode: int, display_id: int) -> str:
        return self._run(self._build_keyevent(keycode, display_id))

    def screencap(self, display_id: int) -> bytes:
        return self._run(self._build_screencap(display_id), binary=True)

    def uidump(self, display_id: int) -> str:
        self._run(self._build_uidump(display_id))
        return self._run(self._build_shell("cat", "/sdcard/ui_dump.xml"))
