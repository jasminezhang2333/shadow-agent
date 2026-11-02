"""淘宝联盟真实比价 provider：调淘宝客 API 搜索推广商品。

环境变量：
- TAOBAO_APP_KEY: 淘宝开放平台 App Key
- TAOBAO_APP_SECRET: 淘宝开放平台 App Secret
- TAOBAO_ADZONE_ID: 推广位 ID（去淘宝联盟后台创建推广位，格式如 116340300225）

权限要求（需在 open.taobao.com 应用详情里申请）：
- taobao.tbk.dg.material.optional（通用物料搜索）

返回格式对齐 PriceProvider 接口：
    [{"platform": "淘宝", "title": "...", "price": 123.45, "url": "..."}]
"""
from __future__ import annotations

import os
import logging

from .comparison_prices import PriceProvider, PriceResult

logger = logging.getLogger(__name__)


class TaobaoPriceProvider:
    """淘宝联盟真实比价：topsdk 调 taobao.tbk.dg.material.optional API。"""

    def __init__(self, app_key: str | None = None, app_secret: str | None = None,
                 adzone_id: str | None = None):
        self.app_key = app_key or os.getenv("TAOBAO_APP_KEY", "")
        self.app_secret = app_secret or os.getenv("TAOBAO_APP_SECRET", "")
        self.adzone_id = adzone_id or os.getenv("TAOBAO_ADZONE_ID", "")

        if not all([self.app_key, self.app_secret, self.adzone_id]):
            raise ValueError(
                "缺少淘宝联盟配置：需要 TAOBAO_APP_KEY / TAOBAO_APP_SECRET / TAOBAO_ADZONE_ID")

    def search(self, query: str, limit: int = 5) -> list[PriceResult]:
        """搜索推广商品（按销量降序），返回标准格式列表。"""
        if not query.strip():
            return []

        try:
            import top
            from top.api.rest.TbkDgMaterialOptionalRequest import TbkDgMaterialOptionalRequest
        except ImportError as e:
            logger.error(f"topsdk 未安装或导入失败: {e}")
            return []

        # 设置全局 app 信息（topsdk 的奇怪设计）
        top.setDefaultAppInfo(self.app_key, self.app_secret)

        req = TbkDgMaterialOptionalRequest()
        req.adzone_id = self.adzone_id
        req.q = query
        req.page_size = min(limit, 20)  # API 最大 100，这里取小点避免超时
        req.page_no = 1
        req.sort = "total_sales_des"  # 按销量降序

        try:
            resp = req.getResponse()
            return self._parse(resp, limit)
        except Exception as e:
            # 权限不足 / 配额超限 / 网络错误都降级成空列表，不让比价炸掉整条链路
            logger.warning(f"淘宝客 API 调用失败: {e}")
            return []

    def _parse(self, resp: str, limit: int) -> list[PriceResult]:
        """解析淘宝客 API 返回的 JSON，提取商品列表。"""
        import json

        try:
            data = json.loads(resp)
        except Exception:
            logger.error(f"淘宝客 API 返回非 JSON: {resp[:200]}")
            return []

        # 正常返回格式：{"tbk_dg_material_optional_response": {"result_list": {"map_data": [...]}}}
        # 错误格式：{"error_response": {"code": 11, "msg": "...", "sub_code": "..."}}
        if "error_response" in data:
            err = data["error_response"]
            logger.warning(
                f"淘宝客 API 业务错误: code={err.get('code')} "
                f"sub_code={err.get('sub_code')} msg={err.get('msg')}")
            return []

        root = data.get("tbk_dg_material_optional_response", {})
        result_list = root.get("result_list", {})
        items = result_list.get("map_data", [])

        results: list[PriceResult] = []
        for item in items[:limit]:
            try:
                title = item.get("title", "").strip()
                price_str = item.get("zk_final_price", "0")
                url = item.get("item_url", "") or item.get("coupon_share_url", "")
                if not title or not url:
                    continue
                results.append(PriceResult(
                    platform="淘宝",
                    title=title,
                    price=float(price_str),
                    url=url,
                ))
            except Exception as e:
                logger.debug(f"解析单个商品失败: {e}, item={item}")
                continue

        return results
