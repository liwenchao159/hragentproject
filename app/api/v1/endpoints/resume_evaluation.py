import logging
from typing import Optional

from app.api.deps import get_current_user

from app.core.database import get_db
from app.schemas.resume_evaluation import ResumeEvaluationListResponse
from app.service.resume_evaluation import ResumeEvaluationService
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
    """
       获取用户的简历评价历史

       - **skip**: 跳过的记录数
       - **limit**: 返回的记录数限制
       - **status**: 状态过滤 (pending, rejected, interview)
       """
    try:
        # 验证状态参数
        status_filter = await ResumeEvaluationService.validate_status_param(status)

        # 限制查询数量
        limit = min(limit, 100)

        evaluation_service = ResumeEvaluationService(db)
        result = await evaluation_service.get_evaluation_history_with_pagination(
            user_id=current_user.id,
            skip=skip,
            limit=limit,
            status=status_filter
        )

        return result

    except ValueError as e:
        logger.warning(f"获取评价历史参数错误: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"获取评价历史失败: {e}")
        raise HTTPException(status_code=500, detail="获取评价历史失败")
