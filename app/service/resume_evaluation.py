import logging
from app.service.dify_service import DifyService
from app.service.llm_service import LLMService
from app.service.resume_parser_service import ResumeParserService
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class ResumeEvaluationService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.dify_service = DifyService()
        self.llm_service = LLMService()
        self.resume_parser = ResumeParserService()
