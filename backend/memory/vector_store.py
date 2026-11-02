"""记忆向量库封装（Chroma）。

一条记忆 = 用户前台屏某一帧被 MLLM 描述后的结构化文本 + metadata。
metadata 带 timestamp / app / 商品实体，方便检索和打通比价。

embedding 默认走 DashScope text-embedding（和 realtime-MLLM 同生态，复用 key），
也支持传入任意 langchain Embeddings 实现（离线可换 HuggingFace 本地模型）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from langchain_core.documents import Document


@dataclass
class MemoryItem:
    text: str
    app: str = ""
    entities: list[str] = field(default_factory=list)
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    image_ref: str = ""  # 可选：帧图落盘路径/URL

    def to_document(self) -> Document:
        return Document(
            page_content=self.text,
            metadata={
                "app": self.app,
                "entities": ",".join(self.entities),
                "timestamp_ms": self.timestamp_ms,
                "image_ref": self.image_ref,
            },
        )


class MemoryStore:
    def __init__(self, persist_dir: str = "./.chroma_memory",
                 collection: str = "screen_memory", embeddings=None):
        from langchain_chroma import Chroma

        if embeddings is None:
            embeddings = self._default_embeddings()
        self._vs = Chroma(
            collection_name=collection,
            embedding_function=embeddings,
            persist_directory=persist_dir,
        )

    @staticmethod
    def _default_embeddings():
        import os

        from langchain_community.embeddings import DashScopeEmbeddings

        return DashScopeEmbeddings(
            model="text-embedding-v3",
            dashscope_api_key=os.getenv("DASHSCOPE_API_KEY", ""),
        )

    def add(self, item: MemoryItem) -> str:
        ids = self._vs.add_documents([item.to_document()])
        return ids[0] if ids else ""

    def add_many(self, items: list[MemoryItem]) -> list[str]:
        if not items:
            return []
        return self._vs.add_documents([i.to_document() for i in items])

    def search(self, query: str, k: int = 4,
               app: str | None = None) -> list[Document]:
        flt = {"app": app} if app else None
        return self._vs.similarity_search(query, k=k, filter=flt)

    def as_retriever(self, k: int = 4):
        return self._vs.as_retriever(search_kwargs={"k": k})
