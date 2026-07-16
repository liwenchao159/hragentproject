import json
import logging
from typing import Any, Optional, Coroutine, AsyncGenerator

from fastapi import APIRouter, HTTPException, status, Form
from fastapi.responses import JSONResponse,StreamingResponse
from fastapi.params import Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.functions import current_user

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.conversation import MessageRole
from app.schemas.user import User as UserSchema
from app.service.converstaion_service import ConversationService
from app.service.knowledge_assistant_service import KnowLedgetAssistantService

router=APIRouter()
logger = logging.getLogger(__name__)

@router.get("/config")
async def get_knowledge_assistant_config(
    db: AsyncSession = Depends(get_db)
):
    """
    获取知识助手配置
    """
    service = KnowLedgetAssistantService(db)
    return await service.get_config()
@router.get("/documents")
async  def get_know_documents(
        knowledge_base_id:str=None,
        skip:int=0,
        limit:int=20,
        current_user:UserSchema=Depends(get_current_user),
        db:AsyncSession=Depends(get_db)
)->Any:
    """获取知识库中的文档列表"""
    service=KnowLedgetAssistantService(db)

    try:
        result=await service.get_documents(
            user_id=current_user.id,
            knowledge_base_id=knowledge_base_id,
            skip=skip,
            limit=limit
        )
        return  result
    except Exception   as e:
        return HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取文档列表错误: {str(e)}"
        )



@router.post("/ask")
async def ask_knowledge_assistant(
        question:str=Form(...),
        knowledge_base_id:str=Form(None),
        context_limit:int=Form(5),
        conversation_history:str=Form("[]"),
        conversation_id:str=Form(...),
        current_user:UserSchema=Depends(get_current_user),
        db:AsyncSession=Depends(get_db)
):
    serice=KnowLedgetAssistantService(db)
    conv_service=ConversationService(db)
    try:
        try:
            conv_history=json.loads(conversation_history) if conversation_history else []
        except json.decoder.JSONDecodeError:
            conv_history=[]
        from uuid import UUID
        try:
            conv_uuid=UUID(conversation_id) if conversation_id else conversation_id
        except Exception:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="无效的会话ID"
                )
        conversation=await  conv_service.get_conversation(conv_uuid,current_user.id)
        if not  conversation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Conversation not found or no permission")
        conv_id=str(conversation.id)

        async  def generate_stream()-> AsyncGenerator[str, Any]:
            try:
                assistant_buffer = ""
                start_sources = []
                async for chunk in serice.ask_question_stream(
                    question=question,
                    user_id=current_user.id,
                    knowledge_base_id=knowledge_base_id,
                    context_limit=context_limit,
                    conversation_history=conv_history
                ):
                    if chunk.get("type")=="start":
                        start_sources=chunk.get("sources") or []
                        payload={**chunk,"conversation_id":conv_id}
                        yield f"data: {json.dumps(payload,ensure_ascii=False)}\n\n"
                    elif chunk.get("type")=="chunk":
                        assistant_buffer+=chunk["content"]
                        yield f"data: {json.dumps(chunk,ensure_ascii=False)}\n\n"
                    elif chunk.get("type")=="end":
                        payload={**chunk,"conversation_id":conv_id}
                        yield f"data: {json.dumps(payload,ensure_ascii=False)}\n\n"
                    else:
                        yield f"data: {json.dumps(chunk,ensure_ascii=False)}\n\n"
                try:
                    from uuid import UUID
                    await conv_service.add_message(
                        conversation_id=UUID(conv_id),
                        content=question,
                        role=MessageRole.USER,
                        context={"knowledge_base_id": str(knowledge_base_id) if knowledge_base_id else None},
                    )
                    await conv_service.add_message(
                        conversation_id=UUID(conv_id),
                        content=assistant_buffer,
                        role=MessageRole.ASSISTANT,
                        context={"sources":start_sources}
                    )
                except Exception as e:
                    logger.error("客户端断开连接，停止生成流式响应")
                    return
            except Exception as e:
                logger.error(f"流式响应生成错误: {str(e)}")
                error_data = {
                    "type": "error",
                    "error": str(e)
                }
                yield  f"data:{json.dumps(error_data,ensure_ascii=False)}\n\n"



        return StreamingResponse(
            generate_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "Cache-Control"
            }
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"生成答案错误: {str(e)}"
        )


