import logging
from typing import Dict, Any

from fastapi import APIRouter, HTTPException,status
from fastapi.params import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.user import User as UserSchemal
from app.service.stats_service import StateService

router=APIRouter()

logger=logging.getLogger(__name__)
@router.get("/dashboard")
async  def  get_dashboard_data(
        current_user:UserSchemal=Depends(get_current_user),
        db:AsyncSession=Depends(get_db)
):
    """获取工作台数据"""

    try:
        stats_service=StateService(db)
        stats=await stats_service.get_dashboard_stats(current_user.id)
        return stats
    except Exception as e:
        logger.error(f"获取工作台数据失败:{e}")
        raise HTTPException(status_code=500,detail="获取工作台数据失败")


@router.get("/recruitment-trend")
async def get_recruitment_trend(
    days: int = 30,
    current_user: UserSchemal = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """
    获取招聘趋势数据
    """
    try:
        stats_service = StateService(db)
        trend_data = await stats_service.get_recruitment_trend_data(str(current_user.id), days)
        return trend_data
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取招聘趋势数据失败: {str(e)}"
        )


@router.get("/training-completion")
async def get_training_completion_stats(
    current_user: UserSchemal = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """
    获取简历评价分布统计
    """
    try:
        stats_service = StateService(db)
        completion_stats = await stats_service.get_training_completion_stats(str(current_user.id))
        return completion_stats
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取简历评价分布统计失败: {str(e)}"
        )


@router.get("/recent-activities")
async def get_recent_activities(
    limit: int = Query(10, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: UserSchemal = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Dict[str, Any]:
    """
    获取最近活动记录（支持分页）
    """
    try:
        stats_service = StateService(db)
        activities = await stats_service.get_recent_activities(str(current_user.id), limit, offset)
        return activities
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"获取最近活动记录失败: {str(e)}"
        )