"""
DashScope API key 池：按当前活跃会话数最少的 key 分配。
不影响 GLM / Gemini，仅给 DashScopeProvider 用。

并发约束：DashScope realtime 对单 key 有并发上限（默认 2）。当所有 key 都满时
acquire() 返回 None，调用方应直接 503 拒绝，避免雪崩重连耗光配额。
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional

from ..config import Settings
from ..utils.logger import logger


class _KeyPool:
    def __init__(self, keys: List[str], max_per_key: int):
        if not keys:
            raise ValueError("DashScope key 池不能为空")
        self._keys: List[str] = list(dict.fromkeys(keys))  # 保序去重
        self._max_per_key: int = max(1, int(max_per_key))
        self._counts: Dict[str, int] = {k: 0 for k in self._keys}
        self._lock = threading.Lock()

    def _short(self, k: str) -> str:
        if len(k) <= 12:
            return k
        return f"{k[:6]}…{k[-4:]}"

    def acquire(self) -> Optional[str]:
        """挑一个未达上限的 key 占用一席。所有 key 都满则返回 None。"""
        with self._lock:
            best = None
            best_n = self._max_per_key
            for k in self._keys:
                n = self._counts[k]
                if n < best_n:
                    best, best_n = k, n
            if best is None:
                logger.warning(
                    f"[keypool] 所有 dashscope key 已满 "
                    f"(per_key={self._max_per_key}, 总容量={self._max_per_key * len(self._keys)})"
                )
                return None
            self._counts[best] += 1
            logger.info(
                f"[keypool] acquire {self._short(best)} "
                f"-> {self._counts[best]}/{self._max_per_key}"
            )
            return best

    def release(self, key: Optional[str]) -> None:
        if not key:
            return
        with self._lock:
            if key in self._counts:
                self._counts[key] = max(0, self._counts[key] - 1)
                logger.info(
                    f"[keypool] release {self._short(key)} "
                    f"-> {self._counts[key]}/{self._max_per_key}"
                )
            else:
                # 配置变更后老 session 用的旧 key 还在释放，忽略
                logger.debug(f"[keypool] release 未知 key {self._short(key)}")

    def snapshot(self) -> Dict[str, int]:
        with self._lock:
            return dict(self._counts)


_pool_singleton: Optional[_KeyPool] = None


def _resolve_keys() -> List[str]:
    keys = list(getattr(Settings, 'DASHSCOPE_API_KEYS', None) or [])
    primary = getattr(Settings, 'DASHSCOPE_API_KEY', None)
    if primary and primary not in keys:
        keys.insert(0, primary)
    return [k for k in keys if k]


def get_pool() -> _KeyPool:
    global _pool_singleton
    if _pool_singleton is None:
        _pool_singleton = _KeyPool(
            keys=_resolve_keys(),
            max_per_key=int(getattr(Settings, 'DASHSCOPE_MAX_PER_KEY', 2)),
        )
    return _pool_singleton


def acquire_key() -> Optional[str]:
    return get_pool().acquire()


def release_key(key: Optional[str]) -> None:
    get_pool().release(key)


def pool_snapshot() -> Dict[str, int]:
    return get_pool().snapshot()
