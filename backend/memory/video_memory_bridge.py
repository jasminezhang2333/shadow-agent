"""子进程桥：把 memory_retrieval 转给 D:\\video-memory 那套 4 层记忆服务。

为什么走子进程而不是 import：
- video-memory 跑在自己的 .venv（有 milvus/numpy 等一堆依赖），shadow-agent 跑在
  ecom-agent conda 环境。两套解释器 + 依赖互不兼容，直接 import 会炸。
- 子进程桥用 video-memory 自己的 python 跑它的脚本，环境彻底隔离；代价是每次查询
  起一个进程（~秒级），对「用户偶尔问一句回忆」的场景完全够用。

约定（见 video-memory/scripts/shadow_bridge.py）：
- stdout = 干净的答复正文，原样当 tool_result。
- 空库/无召回/异常 → 固定兜底句，退出码 0（对本侧不算错误）。
"""
from __future__ import annotations

import os
import subprocess

# video-memory 的位置和它自己的解释器；可用环境变量覆盖，默认按本机现状。
VM_ROOT = os.getenv("VIDEO_MEMORY_ROOT", r"D:\video-memory")
VM_PYTHON = os.getenv(
    "VIDEO_MEMORY_PYTHON", os.path.join(VM_ROOT, ".venv", "Scripts", "python.exe"))
VM_USER = os.getenv("VIDEO_MEMORY_USER", "jake")
VM_BACKEND = os.getenv("VIDEO_MEMORY_BACKEND", "local")

_FALLBACK = "没有找到相关的历史记忆。"


def retrieve(query: str, top_k: int = 4, timeout: float = 30.0) -> str:
    """调 video-memory 的 shadow_bridge.py，拿它 stdout 当答复返回。

    任何失败（找不到解释器、超时、非零退出）都降级成兜底句，绝不抛出——
    memory_retrieval 这条链路要保证手机端总能收到一条 tool_result。
    """
    if not query.strip():
        return _FALLBACK

    script = os.path.join(VM_ROOT, "scripts", "shadow_bridge.py")
    cmd = [
        VM_PYTHON, script,
        "--user", VM_USER,
        "--q", query,
        "--top-k", str(top_k),
        "--backend", VM_BACKEND,
    ]
    env = dict(os.environ)
    env["VM_BACKEND"] = VM_BACKEND
    try:
        proc = subprocess.run(
            cmd, cwd=VM_ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout,
        )
    except FileNotFoundError:
        return _FALLBACK
    except subprocess.TimeoutExpired:
        return _FALLBACK

    out = (proc.stdout or "").strip()
    return out or _FALLBACK


def ingest_video(video_path: str, timeout: float = 300.0) -> tuple[bool, str]:
    """把一段本地 mp4 切片交给 video-memory 抽取+入库（写路径）。

    复用 video-memory 自己的 scripts/build_memory.py --video：它内部就是
    VideoExtractor(qwen3-omni-flash).extract() → MemoryBuilder.build_and_store()，
    用 video-memory 的 .venv 跑，环境彻底隔离。user 必须和读路径一致（默认 jake），
    否则 memory_retrieval 查不到自己刚写进去的记忆。

    返回 (ok, detail)。抽取是花钱的云调用 + 秒级~分钟级，必须在后台 worker 里跑，
    绝不能挂在实时推流的热路径上。
    """
    if not os.path.exists(video_path):
        return False, f"切片不存在: {video_path}"

    script = os.path.join(VM_ROOT, "scripts", "build_memory.py")
    cmd = [
        VM_PYTHON, script,
        "--video", video_path,
        "--user", VM_USER,
        "--backend", VM_BACKEND,
    ]
    env = dict(os.environ)
    env["VM_BACKEND"] = VM_BACKEND
    try:
        proc = subprocess.run(
            cmd, cwd=VM_ROOT, env=env,
            capture_output=True, text=True, encoding="utf-8",
            timeout=timeout,
        )
    except FileNotFoundError:
        return False, "找不到 video-memory 解释器"
    except subprocess.TimeoutExpired:
        return False, "抽取超时"

    detail = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return proc.returncode == 0, detail
