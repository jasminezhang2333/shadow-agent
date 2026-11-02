"""虚拟屏生命周期管理：非 root 下用 scrcpy 3.x --new-display 创建不可见 display。

scrcpy 启动后会在日志里打印新建 display 的 id，形如：
    [server] INFO: New display: 1080x2340/320 (id=2)
我们解析出 id=N 供 AdbBridge 用 `am start --display N` / `input -d N`。

scrcpy 进程本身保持运行以维持虚拟屏存在；可用 --no-video 降低开销
（我们走 ADB screencap 自己取帧，不依赖 scrcpy 的视频窗口）。
"""
from __future__ import annotations

import re
import subprocess
import threading
import time

from .adb_bridge import VirtualDisplay

_DISPLAY_RE = re.compile(r"id=(\d+)")
_NEW_DISPLAY_RE = re.compile(r"New display:\s*(\d+)x(\d+)/(\d+)\s*\(id=(\d+)\)")


class VirtualDisplayManager:
    def __init__(self, serial: str | None = None, scrcpy: str = "scrcpy"):
        self.serial = serial
        self.scrcpy = scrcpy
        self._proc: subprocess.Popen | None = None
        self._display: VirtualDisplay | None = None

    def _build_cmd(self, width: int, height: int, density: int) -> list[str]:
        cmd = [self.scrcpy]
        if self.serial:
            cmd += ["-s", self.serial]
        # --new-display=WxH/dpi 创建虚拟屏；--no-video 不开本地视频窗口省资源
        cmd += [f"--new-display={width}x{height}/{density}", "--no-audio",
                "--no-control"]
        return cmd

    @staticmethod
    def _parse_display(line: str) -> VirtualDisplay | None:
        m = _NEW_DISPLAY_RE.search(line)
        if m:
            w, h, d, did = (int(m.group(i)) for i in (1, 2, 3, 4))
            return VirtualDisplay(display_id=did, width=w, height=h, density=d)
        # 兜底：只抓到 id=
        m2 = _DISPLAY_RE.search(line)
        if m2 and "display" in line.lower():
            return VirtualDisplay(display_id=int(m2.group(1)),
                                  width=0, height=0, density=0)
        return None

    def create(self, width: int = 1080, height: int = 2340,
               density: int = 320, timeout: float = 15.0) -> VirtualDisplay:
        cmd = self._build_cmd(width, height, density)
        self._proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1)
        result: dict[str, VirtualDisplay] = {}

        def _reader():
            assert self._proc and self._proc.stdout
            for line in self._proc.stdout:
                disp = self._parse_display(line)
                if disp:
                    result["d"] = disp
                    break

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        deadline = time.time() + timeout
        while time.time() < deadline and "d" not in result:
            time.sleep(0.1)
        if "d" not in result:
            self.destroy()
            raise TimeoutError("未能从 scrcpy 输出解析到虚拟 display id")
        self._display = result["d"]
        return self._display

    @property
    def display(self) -> VirtualDisplay | None:
        return self._display

    def destroy(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None
        self._display = None
