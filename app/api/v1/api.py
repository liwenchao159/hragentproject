from app.api.v1.endpoints import auth
from fastapi import APIRouter

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["authentication"])


@api_router.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "healthy", "message": "HR Agent API正在运行"}
