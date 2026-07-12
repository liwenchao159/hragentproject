from fastapi import APIRouter
from typing import  Dict,Any

from fastapi.params import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models.user import  User
from app.service.intent_service import IntentService

router=APIRouter()

@router.post("/route")
async  def route_by_intent(
        payload:Dict[str,Any],
        current_user:User=Depends(get_current_user),
        db:AsyncSession=Depends(get_db)
):
    """
    对用户查询进行分类并返回前端路由和意图.
    请求体:{"query":"用户输入内容"}
    """
    query=(payload or {}).get("query","").strip()
    intent_service=IntentService(db)
    result=await  intent_service.route_query(query,current_user.id)
    return result
