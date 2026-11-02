"""京东搜索页比价 provider（临时爬虫版，无需 API Key 审核）。

为什么是"临时版"：
- 淘宝联盟 API 权限被拒，官方路子短期走不通；
- 京东联盟官方接口也要审核，等不起；
- 所以先爬京东搜索页拿真实价格，把链路跑通。
  架构是可插拔的 PriceProvider，以后换京东联盟官方接口只改这一个文件，
  server.py 零改动。

实现要点：
- 搜索页 https://search.jd.com/Search?keyword=... 拿商品 skuId + 标题；
- 价格走单独的价格接口 https://p.3.cn/prices/mgets?skuIds=J_<sku>（京东自家前端也这么拿）；
- 任何失败（反爬/网络/结构变化）都降级成空列表，绝不炸掉整条比价链路。

返回类型严格对齐门面：list[PriceResult]（不是 dict）。
"""
from __future__ import annotations

import logging
import re

from .comparison_prices import PriceProvider, PriceResult

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://search.jd.com/Search"
_PRICE_URL = "https://p.3.cn/prices/mgets"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/120.0.0.0 Safari/537.36"),
    "Referer": "https://www.jd.com/",
}


class MockIphoneProvider(PriceProvider):
    """iPhone 比价 mock，绕过反爬，供测试和演示用。"""

    _RESULTS = [
        PriceResult(
            platform="京东",
            title="Apple iPhone 18 Pro 256GB 原色钛金属 支持移动联通电信5G 双卡双待手机",
            price=8999.0,
            url="https://item.jd.com/100414360094.html",
        ),
        PriceResult(
            platform="Apple 官网",
            title="iPhone 18 Pro · 256GB · 原色钛金属",
            price=9499.0,
            url="https://www.apple.com.cn/shop/buy-iphone/iphone-18-pro",
        ),
        PriceResult(
            platform="天猫",
            title="Apple iPhone 18 Pro 国行 256GB 钛金属系列",
            price=8888.0,
            url="https://detail.tmall.com/item.htm?abbucket=0&id=1081525478551&mi_id=0000CcVx7KfvAu1Ie_JDpXFCP7HqTw3dg8-YSULJpHM0Eoc&rn=9fcec63768802d44b46942f4cb468c59",
        ),
        PriceResult(
            platform="拼多多",
            title="【需当面激活】iPhone 18 PRO 国行新款512G-2T【10天内发货】",
            price=8299.0,
            url="https://m.pinduoduo.com/m_goods-detail?goods_id=1003333445822&goods_name=%E3%80%90%E9%9C%80%E5%BD%93%E9%9D%A2%E6%BF%80%E6%B4%BB%E3%80%91iPhone%2018%20PRO%20%E5%9B%BD%E8%A1%8C%E6%96%B0%E6%AC%BE512G-2T%E3%80%9010%E5%A4%A9%E5%86%85%E5%8F%91%E8%B4%A7%E3%80%91",
        ),
    ]

    def search(self, query: str, limit: int = 5) -> list[PriceResult]:
        q = query.lower()
        if not any(kw in q for kw in ["iphone", "苹果手机", "apple phone"]):
            return []
        return self._RESULTS[:limit]


class JdPriceProvider(PriceProvider):
    """爬京东搜索页 + 价格接口，返回真实商品价格。"""

    def __init__(self, timeout: float = 6.0):
        self.timeout = timeout

    def search(self, query: str, limit: int = 5) -> list[PriceResult]:
        if not query.strip():
            return []
        try:
            import requests
        except ImportError as e:
            logger.error(f"requests 未安装: {e}")
            return []

        try:
            skus = self._search_skus(requests, query, limit)
            if not skus:
                return []
            prices = self._fetch_prices(requests, [s for s, _ in skus])
            results = []
            for sku, title in skus:
                price = prices.get(sku)
                if price is None or price <= 0:
                    continue
                results.append(PriceResult(
                    platform="京东",
                    title=title,
                    price=price,
                    url=f"https://item.jd.com/{sku}.html",
                ))
            return results
        except Exception as e:
            logger.warning(f"京东比价失败，降级空列表: {e}")
            return []

    def _search_skus(self, requests, query: str, limit: int) -> list[tuple[str, str]]:
        """从搜索页 HTML 抽 (skuId, 标题)，按页面顺序取前 limit 个。"""
        resp = requests.get(
            _SEARCH_URL,
            params={"keyword": query, "enc": "utf-8"},
            headers=_HEADERS,
            timeout=self.timeout,
        )
        resp.encoding = "utf-8"
        html = resp.text

        # 每个商品块形如 <li data-sku="100012043978" ... class="gl-item">
        # 标题在块内 <div class="p-name"...><a ...><em>标题</em>
        skus: list[tuple[str, str]] = []
        for m in re.finditer(r'data-sku="(\d+)"', html):
            sku = m.group(1)
            # 从该 sku 位置往后找最近的 p-name em 标题
            title = self._extract_title(html, m.end(), sku)
            skus.append((sku, title))
            if len(skus) >= limit:
                break
        return skus

    @staticmethod
    def _extract_title(html: str, start: int, sku: str) -> str:
        window = html[start:start + 4000]
        m = re.search(r'class="p-name[^"]*".*?<em>(.*?)</em>', window, re.S)
        if not m:
            return f"京东商品 {sku}"
        raw = re.sub(r"<[^>]+>", "", m.group(1))
        return re.sub(r"\s+", " ", raw).strip() or f"京东商品 {sku}"

    def _fetch_prices(self, requests, skus: list[str]) -> dict[str, float]:
        """价格接口批量取价：J_<sku> -> price。返回 {sku: price}。"""
        if not skus:
            return {}
        sku_param = ",".join(f"J_{s}" for s in skus)
        resp = requests.get(
            _PRICE_URL,
            params={"skuIds": sku_param, "type": "1"},
            headers=_HEADERS,
            timeout=self.timeout,
        )
        import json
        data = json.loads(resp.text)
        out: dict[str, float] = {}
        for item in data:
            raw_id = item.get("id", "")           # 形如 "J_100012043978"
            sku = raw_id.split("_", 1)[-1]
            try:
                price = float(item.get("p", "0"))
            except (TypeError, ValueError):
                continue
            if sku and price > 0:
                out[sku] = price
        return out
