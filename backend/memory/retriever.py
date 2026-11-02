"""记忆 RAG 检索：把向量库检索结果喂给 LLM，回答用户的「回忆」类问题。

被 tools/memory_retrieval 调用。当 realtime-MLLM 在用户语音里捕捉到
「回忆/记得/刚才看到」关键词，发来 tool_call{name:"memory_retrieval"} 时，
query 就是用户的问题（如「我刚才看的那双鞋多少钱」）。

无 LLM key 时退化为「只返回检索到的记忆片段」，保证链路可测。
"""
from __future__ import annotations

import os

from langchain_core.documents import Document

from .vector_store import MemoryStore


def _format_docs(docs: list[Document]) -> str:
    lines = []
    for i, d in enumerate(docs, 1):
        ts = d.metadata.get("timestamp_ms", "")
        app = d.metadata.get("app", "")
        lines.append(f"[记忆{i}] (app={app}, ts={ts}) {d.page_content}")
    return "\n".join(lines)


class MemoryRetriever:
    def __init__(self, store: MemoryStore, k: int = 4):
        self.store = store
        self.k = k
        self._llm = self._maybe_llm()

    @staticmethod
    def _maybe_llm():
        key = os.getenv("DASHSCOPE_API_KEY", "")
        if not key:
            return None
        try:
            from langchain_community.chat_models.tongyi import ChatTongyi

            return ChatTongyi(model="qwen-plus", dashscope_api_key=key)
        except Exception:
            return None

    def retrieve(self, query: str, app: str | None = None) -> dict:
        docs = self.store.search(query, k=self.k, app=app)
        context = _format_docs(docs)
        if not docs:
            return {"answer": "没有找到相关的历史记忆。", "sources": []}

        sources = [
            {"text": d.page_content, "metadata": d.metadata} for d in docs
        ]
        if self._llm is None:
            # 退化：直接把命中的记忆拼回去
            return {"answer": f"根据你之前看到的内容：\n{context}",
                    "sources": sources}

        prompt = (
            "你是用户的记忆助手。下面是用户第一人称视角历史画面的记忆片段，"
            "请据此简洁回答用户的问题，只用记忆里真实出现的信息，"
            "不要编造。\n\n"
            f"记忆片段：\n{context}\n\n用户问题：{query}\n\n回答："
        )
        resp = self._llm.invoke(prompt)
        answer = getattr(resp, "content", str(resp))
        return {"answer": answer, "sources": sources}
