from app.api.v1.endpoints import auth, stats, users, conversations, job_description, resume_evaluation, hr_workflows, document, knowledge_base,knowledge_assistant,scoring_criteria
from app.api.v1.endpoints import interview_plan,exam_management,agent
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
api_router.include_router(scoring_criteria.router, prefix="/scoring-criteria", tags=["scoring-criteria"])
api_router.include_router(interview_plan.router, prefix="/interview-plans", tags=["interview-plans"])
api_router.include_router(exam_management.router, prefix="/exam-management", tags=["exam-management"])
api_router.include_router(agent.router, prefix="/agent", tags=["agent"])
@api_router.get("/health")
async def health_check():
    """健康检查端点"""
    return {"status": "healthy", "message": "HR Agent API正在运行"}
