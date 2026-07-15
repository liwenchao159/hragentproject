import asyncio
from typing import Optional

from langchain_core.embeddings import Embeddings
from openai import OpenAI

from app.core.config import settings
import logging

logger = logging.getLogger(__name__)


class CompatibleOpenAIEmbeddings(Embeddings):
    def embed_query(self, text: str) -> list[float]:
        """嵌入查询文本。"""

        try:
            response = self.client.embeddings.create(input=text, model=self.model, dimensions=self.dimension,
                                                     encoding_format="float")
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"嵌入查询时出错: {e}")
            raise

    def __init__(self, api_key: Optional[str],
                 base_url: Optional[str],
                 model: Optional[str],
                 dimension: Optional[int] = 1536,
                 ):
        self.api_key = api_key or settings.EMBEDDING_API_KEY
        self.base_url = base_url or settings.EMBEDDING_BASE_URL
        self.model = model or settings.EMBEDDING_MODEL
        self.dimension = dimension or 1536
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        logger.info(f"DashScop兼容嵌入已使用模型初始化:{self.model}")

    BATCH_SEIZE = 20

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """嵌入搜索文档，自动分批处理以符合API限制"""
        try:
            all_embeddings = []
            for i in range(0, len(texts), self.BATCH_SEIZE):
                batch = texts[i:i + self.BATCH_SEIZE]
                response = self.client.embeddings.create(input=batch, model=self.model, dimensions=self.dimension,
                                                         encoding_format="float")
                sorted_data = sorted(response.data, key=lambda x: x.index)
                all_embeddings.extend([data.embedding for data in sorted_data])
            return all_embeddings
        except Exception as e:
            logger.error(f"嵌入文档时出错: {e}")
            raise

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        """异步嵌入搜索文档。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.embed_documents, texts)

    async def aembed_query(self, text: str) -> list[float]:
        """异步嵌入查询文本。"""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.embed_query, text)
