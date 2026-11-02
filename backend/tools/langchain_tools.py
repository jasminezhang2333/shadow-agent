"""把三个能力包装成 langchain StructuredTool，供 agent 编排层 bind。

同时导出 TOOL_DESCRIPTIONS —— 发给 realtime-MLLM 的 /connect，让 MLLM 知道
有哪些工具可被触发（memory_retrieval / comparison_prices）。phone_act 不暴露给
MLLM，它是 agent 内部动作，不由用户语音触发。
"""
from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from .comparison_prices import ComparisonPrices
from ..memory.retriever import MemoryRetriever


class MemoryQuery(BaseModel):
    query: str = Field(description="用户想回忆的内容，如『刚才看的那双鞋』")


class PriceQuery(BaseModel):
    query: str = Field(description="要比价的商品关键词")


def build_tools(memory_retriever: MemoryRetriever,
                comparator: ComparisonPrices) -> list[StructuredTool]:
    def _memory(query: str) -> str:
        res = memory_retriever.retrieve(query)
        return res["answer"]

    def _prices(query: str) -> str:
        res = comparator.compare(query)
        return res["summary"]

    return [
        StructuredTool.from_function(
            func=_memory, name="memory_retrieval",
            description="检索用户历史画面记忆，回答『回忆/刚才看到』类问题。",
            args_schema=MemoryQuery),
        StructuredTool.from_function(
            func=_prices, name="comparison_prices",
            description="对给定商品做多电商平台比价，返回最低价与对比。",
            args_schema=PriceQuery),
    ]


# 发给 realtime-MLLM /connect 的工具描述（JSON Schema 格式，见 INTERFACE.md §1）
TOOL_DESCRIPTIONS = [
    {
        "name": "memory_retrieval",
        "description": "检索用户第一人称历史画面记忆，回答回忆类问题。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "记忆检索关键词"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "comparison_prices",
        "description": "对画面中出现的商品做多平台比价。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "商品关键词"}
            },
            "required": ["query"],
        },
    },
]
