"""记忆 ingest：前台屏帧 → MLLM 结构化描述 → 入向量库。

两种入口：
1. ingest_description(): 已经有 MLLM 生成好的文本描述（gateway 回调里拿到的），
   直接解析实体后入库。最常用——realtime-MLLM 已经在理解画面了。
2. describe_and_ingest(): 只有一张 JPEG 帧，没有描述时，本地调用一次视觉模型
   生成描述再入库（离线/批处理场景）。

实体抽取用轻量规则 + 可选 LLM；这里给规则兜底，保证无网也能跑测试。
"""
from __future__ import annotations

import re

from .vector_store import MemoryItem, MemoryStore

# 粗粒度商品实体线索：品牌词 / 价格 / 型号模式
_PRICE_RE = re.compile(r"[¥￥]\s?\d+(?:\.\d+)?|\d+\s?元")
_BRAND_HINTS = ["华为", "苹果", "iPhone", "小米", "耐克", "Nike", "阿迪",
                "戴森", "Dyson", "索尼", "Sony", "联想", "三星"]


def extract_entities(text: str) -> list[str]:
    ents: list[str] = []
    for b in _BRAND_HINTS:
        if b.lower() in text.lower():
            ents.append(b)
    ents += _PRICE_RE.findall(text)
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for e in ents:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


class MemoryIngestor:
    def __init__(self, store: MemoryStore):
        self.store = store

    def ingest_description(self, description: str, app: str = "",
                           timestamp_ms: int | None = None,
                           image_ref: str = "") -> str:
        if not description.strip():
            return ""
        item = MemoryItem(
            text=description.strip(),
            app=app,
            entities=extract_entities(description),
            image_ref=image_ref,
        )
        if timestamp_ms is not None:
            item.timestamp_ms = timestamp_ms
        return self.store.add(item)

    def describe_and_ingest(self, jpeg_bytes: bytes, describer,
                            app: str = "") -> str:
        """describer: Callable[[bytes], str]，把帧转成中文描述。
        离线时可注入本地 Qwen-VL / Gemini 视觉调用。"""
        desc = describer(jpeg_bytes)
        return self.ingest_description(desc, app=app)
