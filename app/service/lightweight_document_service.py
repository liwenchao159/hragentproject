from typing import List, Optional
from uuid import UUID

from fastapi import HTTPException,status
from sqlalchemy import select

import logging

from app.schemas.user import User as UserSchema
from app.models.document import Document

logger = logging.getLogger(__name__)


class BaseDocumentService:
    def __init__(self, db):
        self.db = db

    async def get_document_with_permission_check(
            self,
            document_id: str,
            current_user: UserSchema
    ):
        """
        根据ID获取文档并进行权限检查

        参数:
            document_id: 文档ID
            current_user: 当前用户

        返回:
            Document: 文档对象

        异常:
            HTTPException: 404 文档未找到或 403 权限不足
        """
        document=await self.get_by_id(document_id)
        if not document:
            raise HTTPException(404,detail="文档未找到")
        if document.user_id!=current_user.id and not current_user.is_superuser:
            raise HTTPException(
                status_code=403,
                detail="权限不足"
            )
        return document


    async def get_by_id(self, document_id: UUID) -> Optional[Document]:
        try:
            query = select(Document).where(Document.id == document_id)
            result = await self.db.execute(query)
            return result.scalar_one_or_none()
        except HTTPException as e:
            logger.error(f"获取文档{document_id}出错")
            raise
    async def handle_document_service_error(self,exception:Exception,operation:str="操作"):
        """处理文档服务错误并抛出适当的HTTp异常

        参数:
            exception: 捕获的异常
            operation: 操作描述

        异常:
            HTTPException: 适当的HTTP异常
        """
        error_message = str(exception)

        if isinstance(exception, ValueError):
            if "not found" in error_message.lower():
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail="文档未找到"
                )
            elif "permission denied" in error_message.lower():
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="权限不足"
                )
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=error_message
                )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"{operation}时出错: {error_message}"
            )

class LightweightDocumentService(BaseDocumentService):

    def __init__(self, db):
        self.db = db

    async def get_user_documents(self, user_id, skip, limit, category: Optional[str] = None,
                                 knowledge_base_id: Optional[UUID] = None) -> List[Document]:
        try:
            query = select(Document).where(Document.user_id == user_id)
            if category:
                query = query.where(Document.category == category)
            if knowledge_base_id:
                query = query.where(Document.knowledge_base_id == knowledge_base_id)
            query = query.offset(skip).limit(limit).order_by(Document.created_at)

            result = await  self.db.execute(query)
            return result.scalars().all()
        except HTTPException as e:
            logger.error(f"获取用户文档时出错{e}")
            raise
    async def get_by_id(self, document_id: str) -> Optional[Document]:
        try:
            query = select(Document).where(Document.id == document_id)
            result = await self.db.execute(query)
            return result.scalar_one_or_none()
        except HTTPException as e:
            logger.error(f"获取文档{document_id}出错")
            raise

    async def delete_document(self, docuemt_id: str) -> bool:
        try:
            document = await self.get_by_id(docuemt_id)
            if not document:
                return False

            await  self.db.delete(document)
            await self.db.commit()
            return True
        except HTTPException as e:
            logger.error(f"删除文档{docuemt_id}出错")
