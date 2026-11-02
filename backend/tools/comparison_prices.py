"""比价工具：给定商品关键词，返回多平台价格对比。

两种数据来源，优先级从高到低：
1. 真实电商搜索（需配 key / 可接入的比价 API）——留 provider 接口。
2. 离线/演示：返回结构化占位结果，保证无网也能跑通链路演示。

商品实体可来自两处：用户语音里直接说的，或 memory 里存的「刚才看到的商品」。
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass
class PriceResult:
    platform: str
    title: str
    price: float
    url: str = ""

    def as_dict(self) -> dict:
        return {"platform": self.platform, "title": self.title,
                "price": self.price, "url": self.url}


class PriceProvider:
    """真实比价数据源接口。接入淘宝/京东开放平台或第三方比价 API 时实现它。"""

    def search(self, query: str) -> list[PriceResult]:  # pragma: no cover
        raise NotImplementedError


class ComparisonPrices:
    def __init__(self, provider: "PriceProvider | None" = None,
                 providers: "list[PriceProvider] | None" = None):
        # 兼容旧的单 provider 参数；优先用 providers 列表
        if providers is not None:
            self._providers = providers
        elif provider is not None:
            self._providers = [provider]
        else:
            self._providers = []

    def compare(self, query: str) -> dict:
        if not query.strip():
            return {"query": query, "results": [],
                    "summary": "商品关键词为空。"}
        if self._providers:
            results: list[PriceResult] = []
            for p in self._providers:
                try:
                    results.extend(p.search(query))
                except Exception:
                    pass
        else:
            results = self._demo_results(query)

        results = sorted(results, key=lambda r: r.price)
        summary = self._summarize(query, results)
        return {
            "query": query,
            "results": [r.as_dict() for r in results],
            "cheapest": results[0].as_dict() if results else None,
            "summary": summary,
        }

    @staticmethod
    def _demo_results(query: str) -> list[PriceResult]:
        # 演示用：基于关键词 hash 造一组稳定的假价，证明比价/排序/择优逻辑成立
        base = 1000 + (abs(hash(query)) % 4000)
        return [
            PriceResult("京东", f"{query}（京东自营）", base * 1.00,
                        "https://jd.com"),
            PriceResult("淘宝", f"{query}（天猫旗舰店）", base * 0.96,
                        "https://taobao.com"),
            PriceResult("拼多多", f"{query}（百亿补贴）", base * 0.91,
                        "https://pinduoduo.com"),
        ]

    @staticmethod
    def _summarize(query: str, results: list[PriceResult]) -> str:
        if not results:
            return f"没有查到「{query}」的价格。"
        cheapest = results[0]
        return (f"「{query}」最低价在{cheapest.platform}，约 "
                f"¥{cheapest.price:.0f}。共对比 {len(results)} 个平台。")
