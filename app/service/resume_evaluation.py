import logging
from typing import Any, Dict, Optional
from uuid import UUID
from app.models.resume_evaluation import ResumeStatus
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

    async def evaluate_resume(
        self,
        user_id: UUID,
        file_content: bytes,
        filename: str,
        job_description_id: UUID,
        conversation_id: Optional[UUID] = None,
        email_id: Optional[str] = None,
        jd_user_id: Optional[UUID] = None,
    ) -> Dict[str, Any]:
        """评价简历
        Args:
            user_id:评价记录归属的用户ID，
            jd_user_id: JD所属的用户ID，用于查询JD详情。不传则使用user_id
        """
        try:
                        # 1. 验证文件
            is_valid, message = self.resume_parser.validate_file(filename, len(file_content))
            if not is_valid:
                raise ValueError(message)
                        # 2. 提取文本内容
            resume_text = await self.resume_parser.extract_text_from_file(file_content, filename)
            if not resume_text.strip():
                raise ValueError("无法从文件中提取到有效内容")
                      # 3. 获取文件信息
            file_info = self.resume_parser.get_file_info(filename, file_content)  
            
        except Exception as e:
            
    @staticmethod
    async def validate_status_param(status: Optional[str]) -> Optional[ResumeStatus]:
        """验证参数状态"""
        if not status:
            return None
        try:
            return ResumeStatus(status)
        except ValueError:
            raise ValueError("无效的状态值，支持的状态: pending, rejected, interview")

    @staticmethod
    async def get_supported_formats() -> Dict[str, Any]:
        """获取支持的文件格式"""
        return {
            "supported_extensions": [".pdf", ".txt", ".doc", ".docx"],
            "max_file_size": "10MB",
            "description": "支持PDF,TXT,DOC,DOCX格式的简历文件",
        }
