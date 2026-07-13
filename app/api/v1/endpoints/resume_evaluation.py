import logging
from typing import Optional

from app.api.deps import get_current_user

from app.core.database import get_db
from app.schemas.resume_evaluation import ResumeEvaluationListResponse
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/history", response_model=ResumeEvaluationListResponse)
async def get_evaluation_history(
    skip: int = 0,
    limit: int = 20,
    status: Optional[str] = None,
    current_user=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """获取用户的简历评价历史
    - **skip**: 跳过的记录数
    - **limit**: 返回的记录数限制
    - **status**: 状态过滤(pending,rejected,interview)
    """
    try:
        
    except ValueError as e:
        logger.warning(f"获取评价历史参数错误:{e}")
        raise HTTPException(status_code=400,detail=str(e))
    
        
