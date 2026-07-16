from app.api.v1.endpoints import auth, stats, users, conversations, job_description, resume_evaluation, hr_workflows, document, knowledge_base,knowledge_assistant
import app.api.v1.endpoints.intent_router as intent_router
from fastapi import APIRouter

api_router = APIRouter()
api_router.include_router(auth.router, prefix="/auth", tags=["authentication"])
api_router.include_router(intent_router.router, prefix="/intent", tags=["intent"])
api_router.include_router(stats.router, prefix="/stats", tags=["stats"])
api_router.include_router(users.router, prefix="/users", tags=["users"])
api_router.include_router(conversations.router, prefix="/conversations", tags=["conversations"])
api_router.include_router(job_description.router, prefix="/job-descriptions", tags=["job-descriptions"])
api_router.include_router(resume_evaluation.router, prefix="/resume-evaluation", tags=["resume-evaluation"])
api_router.include_router(hr_workflows.router, prefix="/hr-workflows", tags=["hr-workflows"])
api_router.include_router(knowledge_base.router, prefix="/knowledge-base", tags=["knowledge-base"])
api_router.include_router(document.router, prefix="/documents", tags=["documents"])
api_router.include_router(knowledge_assistant.router, prefix="/knowledge-assistant", tags=["knowledge-assistant"])
@api_router.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "healthy", "message": "HR Agent API正在运行"}
