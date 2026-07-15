from http.client import HTTPException
from typing import List, Any

from fastapi import APIRouter
from fastapi.params import Depends, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile

from app.api.deps import get_current_user
from app.core.database import get_db
from app.schemas.document import Document as DocumentSchema, DocumentCreate
from app.schemas.user import User as UserSchema
from app.service.enhanced_document_service import EnhancedDocumentService
from app.service.lightweight_document_service import LightweighDocumentService

router=APIRouter()

@router.get("/",response_model=List[DocumentSchema])
async  def get_documents(
        skip:int=0,
        limit:int=100,
        category:str=None,
        current_user:UserSchema=Depends(get_current_user),
        db:AsyncSession=Depends(get_db)
):
    """获取文档列表"""
    document_service=LightweighDocumentService(db)
    documents=await document_service.get_user_documents(
        user_id=current_user.id,
        skip=skip,
        limit=limit,
        category=category,
    )
    return documents
@router.post("/upload",response_model=DocumentSchema)
async  def upload_document(
        file:UploadFile=File(...),
        category:str=Form(None),
        tags:List[str]=Form(None),
        knowledge_base_id:str=Form(None),
        current_user:UserSchema=Depends(get_current_user),
        db:AsyncSession=Depends(get_db)
)->Any:
    """上传新文档"""
    document_service=EnhancedDocumentService(db)
    try:
        document=await document_service.upload_document(
            file=file,
            user_id=current_user.id,
            category=category,
            tags=tags,
            knowledge_base_id=knowledge_base_id
        )
        return document
    except HTTPException as e:
        await document_service.handle_document_service_error(e, "上传文档")


@router.get("/{document_id}", response_model=DocumentSchema)
async def get_document(
    document_id: str,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """
    根据ID获取文档 - 性能优化版本
    """
    document_service = LightweighDocumentService(db)
    document = await document_service.get_document_with_permission_check(document_id, current_user)
    return document


@router.get("/{document_id}/chunks")
async def get_document_chunks(
        document_id: str,
        current_user: UserSchema = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
) -> Any:
    """
    获取文档分块用于预览
    """
    document_service = EnhancedDocumentService(db)

    try:
        chunks = await document_service.get_document_chunks(document_id, current_user.id)
        return {"chunks": chunks}

    except Exception as e:
        await document_service.handle_document_service_error(e, "获取文档分块")


@router.delete("/{document_id}")
async def delete_document(
        document_id: str,
        current_user: UserSchema = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
) -> Any:
    """
    删除文档
    """
    document_service = EnhancedDocumentService(db)
    document = await document_service.get_document_with_permission_check(document_id, current_user)

    await document_service.delete(document)
    return {"message": "文档删除成功"}

@router.post("/{document_id}/process", response_model=DocumentSchema)
async def process_document(
        document_id: str,
        current_user: UserSchema = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    document_service=EnhancedDocumentService(db)
    try:
        from uuid import UUID
        document=await  document_service.process_document(UUID(document_id))
        return document
    except HTTPException as e:
        await  document_service.handle_document_service_error("处理文档")