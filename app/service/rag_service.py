import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional, Dict, Any, Tuple
from uuid import UUID

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import RunnablePassthrough
from langchain_openai import ChatOpenAI
from langchain_postgres import PGVector
from sqlalchemy import text
from langchain_core.documents import Document as LangChainDocument
from app.core.config import settings
from app.service.embedding_service import get_embedding_service

logger = logging.getLogger(__name__)

# 导入DashScope用于Qwen重排
try:
    import dashscope
    from dashscope import TextReRank

    DASHSCOPE_AVAILABLE = True
except ImportError:
    DASHSCOPE_AVAILABLE = False
    logger.warning("DashScope不可用，Qwen重排将无法工作")


class RankService:
    def __init__(self):
        self.model = None
        self.executor = ThreadPoolExecutor(max_workers=2)
        self._initialize_model()

    def _initialize_model(self):
        """根据配置初始化重排模型"""
        if not settings.RERANK_ENABLED:
            logger.info("配置中已禁用重排")
            return

        if not DASHSCOPE_AVAILABLE:
            logger.error("DashScope不可用，Qwen重排无法工作")
            return

        if not settings.QWEN_API_KEY:
            logger.error("QWEN_API_KEY未设置，Qwen重排无法工作")
            return
        # 使用API密钥初始化DashScope
        dashscope.api_key = settings.QWEN_API_KEY
        self.model = "qwen"
        logger.info("Qwen重排模型初始化成功")

    def _compute_qwen_rerank_scores(self, query, documents) -> List[float]:
        """使用Qwen模型计算文档的重排分数"""
        if not self.model:
            # 如果模型不可用，返回原始顺序分数
            return [1.0 - i * 0.1 for i in range(len(documents))]
        """通过DashScope API使用Qwen模型计算重排分数"""
        if not DASHSCOPE_AVAILABLE or not settings.QWEN_API_KEY:
            logger.error("DashScope不可用或QWEN_API_KEY未设置")
            return [1.0 - i * 0.1 for i in range(len(documents))]

        try:
            response = TextReRank.call(
                model=settings.QWEN_MODEL,
                query=query,
                documents=documents,
                top_n=len(documents),
                return_documents=True
            )
            if response.status_code != 200:
                logger.error(f"Qwen重排API调用失败，状态码 {response.status_code}: {response.message}")
                return [1.0 - i * 0.1 for i in range(len(documents))]

                # 从响应中提取分数
                # 响应应包含带分数的文档列表
            results = response.output.results
            scores = [0.0] * len(documents)
            # 将分数映射回原始文档顺序
            for result in results:
                if 0 <= result.index < len(documents):
                    scores[result.index] = result.relevance_score

            return scores

        except Exception as e:
            logger.error(f"计算Qwen重排分数时出错: {e}")
            # 返回备用分数
            return [1.0 - i * 0.1 for i in range(len(documents))]

    def is_enabled(self) -> bool:
        """检查重排是否启用且模型可用"""
        return settings.RERANK_ENABLED and self.model is not None

    async def rerank_documents(
            self,
            query: str,
            documents: List[LangChainDocument],
            sources: List[Dict[str, Any]],
            top_k: Optional[int] = None
    ) -> Tuple[List[LangChainDocument], List[Dict[str, Any]]]:
        """
        使用Qwen模型根据查询相关性对文档进行重排

        Args:
            query: 搜索查询
            documents: LangChain文档列表
            sources: 源元数据列表
            top_k: 要返回的顶级结果数（默认为RERANK_FINAL_K）

        Returns:
            (重排文档, 重排源)的元组
        """
        if not settings.RERANK_ENABLED or not self.model or not documents:
            return documents, sources

        if top_k is None:
            top_k = settings.RERANK_FINAL_K
        try:
            max_candidates = min(len(documents), top_k)
            candidates_docs = documents[:max_candidates]
            candidate_sources = sources[:max_candidates]
            doc_texts = [doc.page_content for doc in candidates_docs]
            loop = asyncio.get_event_loop()
            rank_scores = await  loop.run_in_executor(
                self.executor,
                self._compute_qwen_rerank_scores,
                query,
                doc_texts,
            )
            # 将文档与其重排分数结合
            doc_score_pairs = []
            for i, (doc, source, rerank_score) in enumerate(zip(candidates_docs, candidate_sources, rank_scores)):
                # 存储原始分数以供比较
                original_score = source.get('combined_score', 0.0)
                # 使用重排信息更新源
                updated_source = source.copy()
                updated_source.update({
                    'rerank_score': float(rerank_score),
                    'original_score': float(original_score),
                    'rerank_enabled': True,
                    'rerank_model': settings.QWEN_MODEL
                })
                doc_score_pairs.append((doc, updated_source, float(rerank_score)))
                # 按重排分数降序排序
                doc_score_pairs.sort(key=lambda x: x[2], reverse=True)
                # 提取top_k结果
                top_pairs = doc_score_pairs[:top_k]
                reranked_docs = [pair[0] for pair in top_pairs]
                reranked_sources = [pair[1] for pair in top_pairs]
                logger.info(f"重排了{len(candidates_docs)}个文档，返回前{len(reranked_docs)}个")

                return reranked_docs, reranked_sources

        except Exception as e:
            logger.error(f"重排过程中出错: {e}")
            # 出错时返回原始结果
            return documents[:top_k], sources[:top_k]


# 全局重排服务实例
_rerank_service = None


def get_rerank_service() -> RankService | None:
    global _rerank_service
    if _rerank_service is None:
        _rerank_service = RankService()
        return _rerank_service


class RAGService:
    def __init__(self, db):
        self.db = db
        # 初始化嵌入服务
        self.embedding_service = get_embedding_service()
        self.embeddings = self.embedding_service.get_embeddings()
        self.llm = ChatOpenAI(
            model=settings.LLM_MODEL,
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
            temperature=0.7,
            max_tokens=2000
        )
        # PGVector的数据库连接字符串
        self.connection_string = settings.DATABASE_URL
        # 初始化重排服务
        self.rerank_service = get_rerank_service()
        logger.info("RAG服务已使用LangChain组件初始化")

    async def ask_question_stream(
            self,
            question: str,
            user_id: UUID,
            knowledge_base_id: Optional[UUID] = None,
            conversation_history: Optional[List[Dict[str, str]]] = None,
            context_limit: int = settings.CONTEXT_LIMIT
    ):
        """
        使用RAG工作流进行流式响应的问题提问

        Args:
            question: 用户问题
            user_id: 用于文档过滤的用户ID
            knowledge_base_id: 可选的知识库ID用于过滤
            conversation_history: 之前的对话消息
            context_limit: 要检索的最大上下文文档数

        Yields:
            包含流式响应数据的字典
        """
        try:
            conversation_history = conversation_history or []
            use_kb = self._should_use_knowledge_base(question)
            print('use_kb=======', use_kb)
            # 如果提供了特定的知识库，总是使用KB检索
            # if knowledge_base_id:
            #     use_kb = True
            logger.info(
                f"ask_question_stream: use_kb={use_kb}, knowledge_base_id={knowledge_base_id}, user_id={user_id}")
            if not use_kb:
                yield {
                    "type": "start",
                    "question": question,
                    "sources": [],
                    "context_used": False,
                    "num_sources": 0
                }
                async for chunk in self._stream_general_chat(question, conversation_history):
                    yield chunk
            print('conversation_history =======', conversation_history)
            # 可选地为KB检索增强查询：查询语句改写
            enhance = self._enhance_query_for_kb(question, conversation_history)
            print('enhance=======', enhance)

            # 改写后的query
            rewritten_query = enhance.get("rewritten_query", question)
            print('rewritten_query=======', rewritten_query)
            # 扩展关键字
            expanded_keywords = enhance.get("expanded_keywords", [])
            print('expanded_keywords=======', expanded_keywords)
            # 为用户的文档创建集合名称（仅块）
            collection_name = f"document_chunks_{user_id}".replace("-", "_")
            # 连接到向量存储
            vector_store = PGVector(
                connection=self.connection_string,
                embeddings=self.embeddings,
                collection_name=collection_name,
                use_jsonb=True
            )
            # 关键词存储已移除；仅保留块向量存储
            # 构建过滤条件
            filter_conditions = {}
            if knowledge_base_id:
                filter_conditions["knowledge_base_id"] = str(knowledge_base_id)
            # 多路径检索：内容（向量）+ 文本（tsvector）
            # 向量路径
            content_results = vector_store.similarity_search_with_relevance_scores(
                rewritten_query, k=context_limit, filter=filter_conditions if filter_conditions else None
            )
            # 第二路径：在块集合上进行PostgreSQL tsvector全文检索
            text_results = await self._tsvector_search(
                collection_name,
                question,
                k=context_limit,
                knowledge_base_id=knowledge_base_id,
                extra_terms=expanded_keywords
            )
            # 合并并选择最终文档和来源
            relevant_docs, sources = await self._merge_docs_with_scores(
                content_results,
                text_results,
                question,
                top_k=context_limit,
                min_similarity_score=settings.RAG_MIN_SIMILARITY_SCORE
            )
            if not relevant_docs:
                yield {
                    "type": "start",
                    "question": question,
                    "query_rewrite": {
                        "rewritten_query": rewritten_query,
                        "expanded_keywords": expanded_keywords
                    },
                    "sources": [],
                    "context_used": False,
                    "num_sources": 0
                }
                async for chunk in self._stream_general_chat(question, conversation_history):
                    yield chunk

            # 使用来源产生初始数据
            yield {
                "type": "start",
                "question": question,
                "query_rewrite": {
                    "rewritten_query": rewritten_query,
                    "expanded_keywords": expanded_keywords
                },
                "sources": sources,
                "context_used": True,
                "num_sources": len(sources)
            }

            # 使用预检索文档创建RAG链（无额外检索）
            rag_chain = self._create_rag_chain_with_docs(relevant_docs, conversation_history)
            try:
                async for chunk in rag_chain.astream({"question": question}):
                    if isinstance(chunk, str):
                        if chunk.strip():
                            yield {"type": "chunk", "content": chunk}
                    if isinstance(chunk, dict):
                        if 'content' in chunk and chunk['content'].strip():
                            yield {"type": "chunk", "content": chunk['content']}
                        if 'output' in chunk and chunk['output'].strip():
                            yield {"type": "chunk", "content": chunk['content']}
            except GeneratorExit:
                logger.info("客户端断开连接，停止RAG流式响应")
                return
            except Exception as e:
                logger.error(f"RAG流式响应生成错误：{str(e)}")
                yield {"type": "error", "error": str(e)}
            yield {
                "type": "end",
                "complete": True,
                "sources": sources,
                "num_sources": len(sources)
            }
        except Exception as e:
            logger.error(f"流式RAG问答中出错: {e}")
            yield {
                "type": "error",
                "error": str(e)
            }

    def _should_use_knowledge_base(self, question):
        """决定用户问题是否应该使用知识库
        如果返回True使用kb返回false不适用
        """

        try:
            # 先做关键词预筛：纯闲聊关键词直接走 GENERAL，避免浪费 LLM 调用
            chitchat_keywords = ["你好", "谢谢", "再见", "你是谁", "讲个笑话", "作首诗", "写首诗", "聊天"]
            question_stripped = question.strip()
            if any(question_stripped == kw for kw in chitchat_keywords):
                logger.info(f"关键词预筛选命中闲聊，跳过KB检索:{question}")
                return False

            classification_prompt = (
                    f"""
            你是严格二分类器，仅二选一输出，**绝对不能输出任何多余文字、标点、换行、解释**。
            规则：
            1. 只有纯粹闲聊、打招呼、娱乐需求（问好、讲笑话、作诗、自我介绍）输出单词：GENERAL
            2. 所有业务、文档、资料、查询类问题，以及无法判断的模糊问题，统一输出单词：KB

            示例：
            问：讲个笑话
            答：GENERAL
            问：你好
            答：GENERAL
            问：你是谁
            答：GENERAL
            问：作首诗
            答：GENERAL
            问：这个项目流程是什么
            答：KB

            硬性要求：只能单独输出 KB 或 GENERAL，不能加任何其他字符。
            问：{question}
            答："""
            )
            # 复用类全局已初始化的llm，不再新建实例
            resp = self.llm.invoke(classification_prompt)
            content = getattr(resp, "content", "")
            # 清洗输出
            clean_content = content.strip().upper()
            logger.info(
                f"KB意图分类结果:question='{question[:50]}...',模型原始输出='{content}',清洗后='{clean_content}'")

            content = getattr(resp, "content", str(resp))
            logger.info(f"KB意图分类结果:question='{question[:50]}...',response='{content}'")

            if 'GENERAL' in (content or "").upper() and 'KB' not in (content or "").upper():
                return False
            return True

        except Exception as e:
            logger.warning(f"KB意图检测失败,默认使用KB:{e}")
            return True

    def _create_general_chat_chain(self, conversation_history):
        """不带kb上下文通用聊天链"""
        from langchain_core.messages import HumanMessage, AIMessage
        formated_history = []
        for msg in conversation_history:
            if msg.get("role") == "user":
                formated_history.append(HumanMessage(msg.get("content", "")))
            elif msg.get("role") == "assistant":
                formated_history.append(AIMessage(msg.get("content", "")))
        system_prompt = (
            """
            你是一个人工智能助手。直接根据用户问题进行回答，不使用任何上下文。
            保持回答准确、简介、有帮助
            """
        )
        prompt = ChatPromptTemplate.from_messages(
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{question}")
        )
        chain = ({
                     "question": RunnablePassthrough(),
                     "cha_history": lambda x: formated_history
                 } | prompt | self.llm | StrOutputParser())
        return chain

    def _enhance_query_for_kb(
            self,
            question: str,
            conversation_history: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        基于LLM的语义增强：重写查询并提供扩展关键词。
        返回: { "rewritten_query": str, "expanded_keywords": List[str] }

        """
        try:
            if not getattr(settings, "KB_QUERY_ENHANCE_ENABLED", False):
                return {"rewritten_query": question, "expanded_keywords": []}
            system_prompt = (
                """"
                你是一个检索查询增强器。
                通过对上下文理解，理解用户的真实意图，输出更清晰的检索查询及若干关键术语扩展。
                返回严格的JSON对象：{{\"rewritten_query\":\"...\",\"expanded_keywords\":[\"...\"]}}
                注意：扩展术语需要短而精准，避免长句子。
                """
            )
            conversation_history = conversation_history or []
            prompt = ChatPromptTemplate.from_messages([
                ("system", system_prompt),
                MessagesPlaceholder(variable_name="chat_history"),
                ("human", "原始查询:{question}]\n请返回JSON格式结果，不要返回JSON外的其他任何说明或解释")
                ]
            )
            enhancer_llm = ChatOpenAI(
                model=settings.LLM_MODEL,
                api_key=settings.LLM_API_KEY,
                base_url=settings.LLM_BASE_URL,
                temperature=0.2,
                max_tokens=1024
            )
            chain = (
                    {
                        "question": RunnablePassthrough(),
                        "chat_history": lambda x: conversation_history
                    } | prompt | enhancer_llm | StrOutputParser()
            )
            raw = chain.invoke(question)
            rewritten_query = question
            expanded_keywords: List[str] = []
            try:
                import json as pyjson
                data = pyjson.loads(raw)
                rewritten_query = data.get("rewritten_query") or question
                ek = data.get("expanded_keywords") or []
                if isinstance(ek, list):
                    expanded_keywords = [str(t).strip() for t in ek if str(t).strip()]
                elif isinstance(ek, str):
                    expanded_keywords = [t.strip() for t in ek.split(',') if t.strip()]
            except Exception as e:
                terms = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", raw)
                expanded_keywords = [t.lower() for t in terms if len(t) >= 2]
            max_terms = getattr(settings, "KB_QUERY_EXPANSION_MAX_TERMS", 6)
            if len(expanded_keywords) > max_terms:
                expanded_keywords = expanded_keywords[:max_terms]
            return {"rewritten_query": rewritten_query, "expanded_keywords": expanded_keywords}


        except Exception as e:
            logger.warning(f"查询增强失败: {e}")
            return {"rewritten_query": question, "expanded_keywords": []}

    async def _tsvector_search(
            self,
            collection_name: str,
            query: str,
            k: int = 5,
            knowledge_base_id: Optional[UUID] = None,
            extra_terms: Optional[List[str]] = None
    ) -> List[tuple]:
        """
        基于langchain_pg_embedding.document的PostgreSQL全文检索（tsvector）。
        - 集合名严格过滤：cmetadata->>'collection_name'
        - 查询重写为 OR 前缀 tsquery（类似Elasticsearch match：任一词命中即返回）
        - 对中文/英中混合词保留 ILIKE 后备，提高召回率
        返回 (LangChainDocument, score) 列表，与向量检索输出格式一致。
        """
        try:
            # 1)查询重写 提取英文/数字/中文词元 移除常见问句停用词，构造OR前缀tsquery
            stop_words = {"有哪些", "什么", "如何", "怎么", "请问", "的", '和', '与'}
            terms = re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", query)
            terms = [t.lower() for t in terms if t not in stop_words and len(t) >= 2]
            if extra_terms:
                for t in extra_terms:
                    t = str(t).strip().lower()
                    if t and t not in terms and t not in stop_words:
                        terms.append(t)
            # 构造 tsquery
            tsquery_or = " | ".join(f"{t}:*" for t in terms) if terms else None
            base_sql = (
                "SELECT id, document, cmetadata, "
                "ts_rank_cd(to_tsvector('simple', document), to_tsquery('simple', :tsq)) AS rank "
                "FROM langchain_pg_embedding "
                "WHERE cmetadata->>'collection_name' = :collection_name "
                "AND ("
                "     ( :tsq IS NOT NULL AND to_tsvector('simple', document) @@ to_tsquery('simple', :tsq) ) "
                "     OR document ILIKE '%' || :q || '%'"
                ") "
            )
            params = {"q": query, "tsq": tsquery_or, "collection_name": collection_name, "limit": k}

            if knowledge_base_id:
                base_sql += "AND cmetadata->>'knowledge_base_id' = :kb_id "
                params["kb_id"] = str(knowledge_base_id)
            base_sql += "ORDER BY rank DESC LIMIT :limit"

            res2 = await self.db.execute(text(base_sql), params)
            rows = res2.fetchall()
            results: List[tuple] = []
            for r in rows:
                doc_text = r[1]
                metadata = r[2] or {}
                page_content = doc_text
                lc_doc = LangChainDocument(page_content=page_content, metadata=metadata)
                results.append((lc_doc, float(r[3]) if r[3] is not None else 0.0))
            return results
        except Exception as e:
            logger.warning(f"tsvector搜索错误: {e}")
            return []

    async def _merge_docs_with_scores(
            self,
            content_results,
            text_results,
            query,
            top_k,
            min_similarity_score):
        """
        使用可配置的融合方法合并向量内容结果和PostgreSQL tsvector结果。
        支持RRF（倒数排名融合）和加权和方法，可选择重排。
        """
        try:
            merged_map: Dict[tuple, Dict[str, Any]] = {}

            # 收集向量搜索结果
            for doc, score in content_results:
                key = (doc.metadata.get("document_id"), doc.metadata.get("chunk_index"))
                merged_map[key] = merged_map.get(key, {"doc": doc, "content_score": 0.0})
                merged_map[key]["doc"] = doc
                merged_map[key]["content_score"] = float(score)

            # 集成tsvector文本搜索结果
            for doc, score in text_results:
                key = (doc.metadata.get("document_id"), doc.metadata.get("chunk_index"))
                entry = merged_map.get(key, {"doc": doc, "content_score": 0.0})
                entry["doc"] = entry.get("doc") or doc
                entry["text_score"] = float(score)
                merged_map[key] = entry

            # 合并分数：优先考虑向量相似性，然后是关键词匹配
            combined_list: List[tuple] = []
            for key, entry in merged_map.items():
                content_score = float(entry.get("content_score", 0.0))
                text_score = float(entry.get("text_score", 0.0))
                # 加权组合：使用配置文件中的权重
                combined_score = (settings.RAG_CONTENT_WEIGHT * content_score +
                                  settings.RAG_TEXT_WEIGHT * text_score)
                combined_list.append((entry["doc"], combined_score, entry))

            # 按combined_score降序排序（更高相关性优先）
            combined_list.sort(key=lambda x: x[1], reverse=True)
            combined_list = [item for item in combined_list if float(item[1]) >= min_similarity_score]
            # 构建输出
            top = combined_list[:top_k]
            docs: List[LangChainDocument] = []
            sources: List[Dict[str, Any]] = []
            for doc, combined_score, entry in top:
                final_page_content = doc.page_content
                final_doc = LangChainDocument(page_content=final_page_content, metadata=doc.metadata)
                docs.append(final_doc)
                sources.append({
                    "document_id": doc.metadata.get("document_id"),
                    "document_title": doc.metadata.get("filename", "Unknown"),
                    "chunk_id": doc.metadata.get("chunk_id"),
                    "chunk_index": doc.metadata.get("chunk_index", 0),
                    "content": final_page_content,
                    "combined_score": float(combined_score),
                    "content_score": float(entry.get("content_score", 0.0)),
                    "text_score": float(entry.get("text_score", 0.0)),
                    "metadata": doc.metadata
                })

                # 如果启用则应用重排
            if settings.RERANK_ENABLED and self.rerank_service.is_enabled():
                docs, sources = await self.rerank_service.rerank_documents(
                    query=query,
                    documents=docs,
                    sources=sources,
                    top_k=top_k
                )
            else:
                # 如果没有重排则只取top_k
                docs = docs[:top_k]
                sources = sources[:top_k]

            return docs, sources
        except Exception as e:
            logger.warning(f"合并多路径结果时出错: {e}")
            # 备用方案：仅返回content_results
            docs = [doc for doc, _ in content_results[:top_k]]
            sources = []
            for doc, score in content_results[:top_k]:
                sources.append({
                    "document_id": doc.metadata.get("document_id"),
                    "document_title": doc.metadata.get("filename", "Unknown"),
                    "chunk_id": doc.metadata.get("chunk_id"),
                    "chunk_index": doc.metadata.get("chunk_index", 0),
                    "content": doc.page_content,
                    "combined_score": float(score),
                    "content_score": float(score),
                    "text_score": 0.0,
                    "metadata": doc.metadata
                })
            return docs, sources

    async def _stream_general_chat(self, question: str, conversation_history: List[Dict[str, Any]]) -> Any:
        """不带知识库大模型自动生成"""
        try:
            general_chain = self._create_general_chat_chain(conversation_history)
            async for chunk in general_chain.astream({"question": question}):
                if isinstance(chunk, str):
                    if chunk.strip():
                        yield {"type": "chunk", "content": chunk}
                elif isinstance(chunk, dict):
                    if 'content' in chunk and chunk['content'].strip():
                        yield {"type": "chunk", "content": chunk['content']}
                    elif 'output' in chunk and chunk['output'].strip():
                        yield {'type': "chunk", "content": chunk['output']}
                yield {"type": "end", 'complete': True, 'sources': [], 'num_sources': 0}
        except GeneratorExit:
            logger.info('客户端断开连接，停止流式响应')
            return
        except Exception as e:
            logger.error(f"流式响应生成错误:{e}")
            return

    def _create_rag_chain_with_docs(self, docs: List[LangChainDocument], conversation_history: List[Dict[str, str]]):
        from langchain_core.messages import HumanMessage, AIMessage
        formatted_history = []
        for msg in conversation_history:
            if msg.get("role") == "user":
                formatted_history.append(HumanMessage(content=msg.get("content", "")))
            elif msg.get("role") == "assistant":
                formatted_history.append(AIMessage(content=msg.get("content", "")))
        system_prompt = """"
        你是一个智能助手，基于提供的上下文信息回答用户问题.
        上下文信息：
        {context}
        
        请根据上下文信息回答用户的问题。如果上下文信息不足以回答问题，请诚实地说明。
        保持回答准确，有用且简洁。
        """
        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            MessagesPlaceholder(variable_name="chat_history"),
            ("human", "{question}")
        ])

        # 格式化文档
        def format_docs(docs_list):
            return "\n\n".join(doc.page_content for doc in docs_list)

        rag_chain = (
                {
                    "context": lambda x: format_docs(docs),
                    "question": RunnablePassthrough(),
                    "chat_history": lambda x: formatted_history
                } | prompt
                | self.llm
                | StrOutputParser()
        )
        return rag_chain
