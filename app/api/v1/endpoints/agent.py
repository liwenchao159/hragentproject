import json
import logging
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.api.deps import get_current_user
from app.core.database import get_db
from app.schemas.agent import AgentChatRequest
from app.schemas.user import User as UserSchema
from app.service.agent_service import AgentService

logger=logging.getLogger(__name__)

router=APIRouter()

@router.post("/chat/stream")
async def stream_chat_with_agent(
    request: AgentChatRequest,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """流式处理 HR Agent 自然语言任务"""

    async def generate():
        try:
            agent_service = AgentService(db)
            async for event in agent_service.stream_chat_agent(
                message=request.message.strip(),
                user_id=current_user.id,
                conversation_id=request.conversation_id,
                auto_execute=request.auto_execute,
                confirmed_requirements=request.confirmed_requirements,
                attachments=[item.model_dump() for item in request.attachments],
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as exc:
            error_event = {"type": "error", "error": f"HR Agent 执行失败: {str(exc)}"}
            yield f"data: {json.dumps(error_event, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )