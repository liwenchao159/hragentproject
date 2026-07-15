import hashlib
import mimetypes
import os.path
import tempfile
from typing import Optional, List, BinaryIO, Dict, Any
from uuid import UUID

from langchain_community.vectorstores import PGVector
from langchain_core.documents import Document as LangChainDocument
from fastapi import  UploadFile
from sqlalchemy import select, text

from app.core import logging
from app.core.config import settings
from app.models import Document
from app.service.lightweight_document_service import BaseDocumentService
from app.service.llm_service import LLMService
from app.utils.text_utils import extract_text_content

logger = logging.getLogger(__name__)


class EnhancedDocumentService(BaseDocumentService):
    def __init__(self, db):
        super().__init__(db)
        self.db = db
        self.llm_service = LLMService()

    async def upload_document(self, file: UploadFile,
                              user_id: UUID, category: Optional[str] = None, tags: Optional[List[Optional[str]]] = None,
                              knowledge_base_id: Optional[str] = None) -> Document:
        """上传文档并保存文档信息"""
        kb_id = None
        if knowledge_base_id:
            try:
                kb_id = UUID(knowledge_base_id)
            except(ValueError, TypeError):
                logger.warning(f"无效的知识库ID格式:{knowledge_base_id}")
                kb_id = None
        return await self.upload_and_process_document(
            file=file.file,
            filename=file.filename,
            user_id=user_id,
            knowledge_base_id=kb_id,
            category=category,
            tags=tags
        )

    async def upload_and_process_document(self, file: BinaryIO,
                                          filename: str,
                                          user_id: UUID,
                                          knowledge_base_id: Optional[UUID] = None,
                                          category: Optional[str] = None,
                                          tags: Optional[List[Optional[str]]] = None) -> Document:
        """上传文档（仅保存文件，不进行解析和向量化）"""
        try:
            file_content = file.read()
            file.seek(0)
            file_hash = hashlib.sha256(file_content).hexdigest()
            existing_doc = await  self._get_document_by_hash(file_hash, user_id)
            if existing_doc:
                logger.info(f"哈希为 {file_hash} 的文档已存在")
                return existing_doc
            # 确定MIME类型
            mime_type = self._get_mime_type(filename)
            # 永久保存文件
            file_path = await  self._save_file(file_content, filename, user_id)
            document = Document(
                filename=filename,
                original_filename=filename,
                file_path=file_path,
                file_hash=file_hash,
                mime_type=mime_type,
                extracted_content=None,
                embedding=None,
                category=category,
                tags=tags,
                knowledge_base_id=knowledge_base_id,
                user_id=user_id,
            )
            self.db.add(document)
            await  self.db.commit()
            await self.db.refresh(document)
            logger.info(f"文档上传成功:{document.id}")
            return document
        except Exception as e:
            logger.error(f"上传文档时出错{e}")
            await self.db.rollback()
            raise e

    async def _get_document_by_hash(self, file_hash, user_id):
        """根据文件哈希获取文档"""
        query = select(Document).where(
            Document.file_hash == file_hash,
            Document.user_id == user_id
        )
        result = await self.db.execute(query)
        return result.scalar_one_or_none()

    def _get_mime_type(self, filename):
        """根据文件名确定MIME类型"""
        extension = filename.lower().split('.')[-1] if '.' in filename else ''
        mime_types = {
            'pdf': 'application/pdf',
            'doc': 'application/msword',
            'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
            'txt': 'text/plain',
            'md': 'text/markdown',
            'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'xls': 'application/vnd.ms-excel'
        }
        return mime_types.get(extension, 'application/octet-stream')

    async def _save_file(self, file_content, filename, user_id):
        upload_dir = os.path.join(settings.UPLOAD_DIR, str(user_id))
        os.makedirs(upload_dir, exist_ok=True)

        file_path = os.path.join(upload_dir, filename)
        counter = 1
        base_name, extension = os.path.splitext(filename)
        while os.path.exists(file_path):
            newfile_name = f"{base_name}_{counter}{extension}"
            file_path = os.path.join(upload_dir, newfile_name)
            counter += 1
        with open(file_path, 'wb') as f:
            f.write(file_content)
        return file_path

    async def get_document_chunks(self, document_id: str, user_id: UUID) -> List[Dict[str, Any]]:
        """获取文档分块"""
        try:
            # 首先检查文档是否存在且用户有权限
            document = await  self.get_by_id(document_id)
            if not document:
                raise ValueError("文档未找到")
            if document.user_id != user_id:
                raise ValueError("权限被拒绝")
            query = text("""
                SELECT id,document,cmetadata from  langchain_pg_embedding
                WHERE cmetadata->>'document_id'=:document_id
                ORDER BY CAST(cmetadata->>'chunk_index' AS INTEGER)
            """)
            result = await self.db.execute(query, {"document_id": document.id})
            rows = result.fetchall()
            # 格式化块用于响应
            formatted_chunks = []
            for row in rows:
                metadata = row.cmetadata if row.cmetadata else {}
                formatted_chunks.append(
                    {
                        "id": str(row.id),
                        "content": row.document,
                        "chunk_index": metadata.get("chunk_index", 0),
                        "chunk_size": metadata.get("chunk_size", len(row.document) if row.document else 0),
                        "metadata": metadata
                    }
                )
            return formatted_chunks
        except Exception as e:
            logger.error(f"获取文档chunk出错:{e}")
            raise

    async def delete(self, document):
        """从langchin_pg_embedding删除文档及其块"""
        try:
            delete_query = text("""
                DELETE FROM langchain_pg_embedding
                WHERE id=:document_id
            """)
            await self.db.execute(delete_query, {"document_id": str(document.id)})
            # 删除文档
            await self.db.delete(document)
            await self.db.commit()

            # 如果文件存在则清理
            if document.file_path and os.path.exists(document.file_path):
                os.unlink(document.file_path)
            logger.info(f"已删除文档 {document.id} 机器嵌入向量")
        except Exception as e:
            logger.error(f"删除文档时出错:{e}")
            await self.db.rollback()
            raise

    async def process_document(self, document_id: UUID):

        result = await self.db.execute(select(Document).where(Document.id == document_id))
        document = result.scalar_one_or_none()
        if not document:
            raise ValueError(f"文档不存在:{document_id}")

        temp_file_path = None
        try:
            # 临时文件创建以进行文本提取
            with open(document.file_path, 'rb') as f:
                file_content = f.read()
            temp_file_path = await self._save_temp_file(file_content, document.filename)
            # 提取文件内容
            extract_content = await extract_text_content(temp_file_path, document.mime_type)

            document.extracted_content = extract_content
            if extract_content:
                await self._create_document_chunks_with_pgvector(document, extract_content)

    async def _save_temp_file(self, file_content, filename):
        """创建临时文件"""
        temp_dir = tempfile.gettempdir()
        temp_file = os.path.join(temp_dir, filename)
        with open(temp_file, 'wb') as f:
            f.write(file_content)

        return temp_file

    async def _create_document_chunks_with_pgvector(self, document, extract_content):
        """使用PGVector为嵌入向量创建文档块"""
        text_chunks = await self._split_text(extract_content)
        if not text_chunks:
            logger.warning(f"未为文档 {document.id} 提供文本块")
            return
        # 创建带有元数据的langchain文档（内容块）
        chunks_collection=f"document_chunks_{document.user_id}".replace("-","_")
        langchain_docs = []
        for i,chunk_text in text_chunks:
            doc=LangChainDocument(
                page_content=chunk_text,
                metadata={
                    "document_id": str(document.id),
                    "knowledge_base_id": str(document.knowledge_base_id),
                    "chunk_index": i,
                    "chunk_size": len(chunk_text),
                    "filename": document.filename,
                    "category": document.category or "general",
                    "file_path": document.file_path,
                    "mime_type": document.mime_type,
                    "source_type": "content",
                    "collection_name": chunks_collection
                }
            )
            langchain_docs.append(doc)

        # 获取向量存储
        vector_store=PGVector(
            connection=self.connection_string
        )

    async def _split_text(self, extract_content):
        """主分割流程：使用 LLM 分割点 + 切分 + 长度约束"""
        # 1) 获取分割点（可能为空）
        points = await self.get_semantic_split_points(extract_content)
        # 2)根据分割点分割文档
        if points:
            chunks = self._split_by_semantic_points(extract_content, points)
        else:
            chunks = [extract_content]
        # 3)对超长片段进行强制分割>1000
        normalized: List[str] = []
        for chunk in chunks:
            if len(chunk) > 1000:
                normalized.extend(self._force_split_long_chunk(chunk))
            else:
                normalized.append(chunk)
        # 4)合并过段片段<50
        normalized = self._merge_short_chunks(normalized, min_length=50, max_length=1000)
        # 5) 去除空白
        normalized = [c.strip() for c in normalized if c and c.strip()]
        return normalized

    async def get_semantic_split_points(self, extract_content):
        """获取文档语义对应的拆分点"""
        try:
            system_prompt = (
                """你是一个文档结构分析助手。只用于输出文档Split的分割点字符串,
                用'~~'分割，但不要输入任何其他文字。确保每个分割点在原文中唯一，
                如果遇到重复标题或目录项，需要在分割点追加少量后续字符形成唯一片段"""
            )
            user_prompt = (
                f""""
               # 任务
                  请分析文档，识别试合作为分割点的文本片段
                  
                  
               # 规则
                 1) 分割点应位于章节、各级标题、句子、或段落的开头;
                 2) 分割后的每段内容尽量<500字节，严禁>800字节;
                 3) 若存在重复片段（例如目录与正文相同标题）,需要在分割点后追加少量后续内容以确保唯一;
                 4) 仅输入分割点字符串,使用‘~~’分割，不要解释或添加其他任何文本
               # 文档内容
                 {extract_content[:10000]}
               """
            )
            response = await self.llm_service.client.chat.completions.create(
                model=self.llm_service.llm_model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=1000
            )
            raw = response.choices[0].message.content or ""
            points = [p.strip() for p in raw.split('~~') if p.strip()]
            if not points:
                return []

            # 去重分割点
            seen = set()
            unique_points = []
            for p in points:
                if p not in seen:
                    seen.add(p)
                    unique_points.append(p)

            def ensure_unique(point: str) -> str:
                start = extract_content.find(point)
                if start == -1:
                    return ""

                count = 0
                search_pos = 0
                while True:
                    idx = extract_content.find(point, search_pos)
                    if idx == -1:
                        break
                    count = count + 1
                    search_pos = idx + 1
                if count <= 1:
                    return point
                # 重复:逐步扩展片段 直到唯一或道道限制
                # 最多追加100个字符，步长10
                max_extra = 100
                step = 10
                extra = 0
                while extra < max_extra:
                    candidate = extract_content[start:start + len(point) + extra]
                    if len(candidate) <= len(point):
                        extra = extra + step
                        continue

                    c = 0
                    sp = 0
                    while True:
                        j = extract_content.find(candidate, sp)
                        if j == -1:
                            break
                        c = c + 1
                        sp = j + 1
                    if c <= 1:
                        return candidate
                    extra = extra + step
                return point

            adjusted_points_with_index: List[tuple[int, str]] = []
            for p in unique_points:
                adj = ensure_unique(p)
                if not adj:
                    continue
                idx = extract_content.find(adj)
                if idx != -1:
                    adjusted_points_with_index.append((idx, adj))
            # 按在正文中的出现位置的顺序进行排序
            adjusted_points_with_index = sorted(adjusted_points_with_index, key=lambda x: x[0])
            final_points = [pt for _, pt in adjusted_points_with_index]
            return final_points
        except Exception as e:
            logger.error(f"增强文档服务获取予以分割点出错")
            return []

    def _split_by_semantic_points(self, text: str, points: List[str]):
        """根据语义分割点切分文本"""
        chunks = []
        current_pos = 0
        for point in points:
            pos = text.find(point)
            if pos != -1:
                if pos > current_pos:
                    chunk = text[current_pos:pos].strip()
                    if chunk:
                        chunks.append(chunk)
                current_pos = pos
        if current_pos < len(text):
            chunk = text[current_pos:].strip()
            if chunk:
                chunks.append(chunk)
        return chunks

    from typing import List

    def _merge_short_chunks(self, chunks: List[str], min_length: int, max_length: int) -> List[str]:
        merged, idx, total = [], 0, len(chunks)
        while idx < total:
            cur = chunks[idx]
            if len(cur) >= min_length:
                merged.append(cur)
                idx += 1
                continue
            buf, nxt = cur, idx + 1
            while nxt < total and len(buf) < min_length:
                sep = "\n" if not buf.endswith("\n") else ""
                cand = buf + sep + chunks[nxt]
                if len(cand) > max_length:
                    break
                buf, nxt = cand, nxt + 1
            if nxt > idx + 1:
                merged.append(buf)
                idx = nxt
            else:
                sep = "\n" if not cur.endswith("\n") else ""
                merged.append(cur + sep + chunks[idx + 1])
                idx += 2
        return merged

    def _force_split_long_chunk(self, chunk:str)->List[str]:
        """强制分割超长段落（超过1000字符）"""
        max_length = 1000
        chunks = []
        if '\n' in chunk:
            lines = chunk.split('\n')
            current_chunk = ""
            for line in lines:
                if len(current_chunk) + len(line)+1 > max_length:
                    if current_chunk:
                        chunks.append(current_chunk)
                    else:
                        # 单行就超过最大长度，需要递归分割
                        line_chunks = self._force_split_long_chunk(line)
                        chunks.extend(line_chunks)
                        current_chunk = ""
                else:
                    if current_chunk:
                        current_chunk+="\n"+line
                    else:
                        current_chunk=line
        else:
            chunks=[chunk[i:i+max_length] for i in range[0,len(chunk),max_length]]


        return chunks




