import os
import tempfile
from typing import Tuple
import logging

from app.utils.file_utils import get_file_mime_type

logger = logging.getLogger(__name__)


class ResumeParserService:
    """用于解析简历文件和提取文本内容的服务"""

    SUPPORTED_EXTENSIONS = {".pdf", ".txt", ".doc", ".docx"}
    SUPPORTED_MIME_TYPES = {
        "application/pdf",
        "text/plain",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }

    def __init__(self):
        self.max_file_size = 10 * 1024 * 1024  # 10MB

    def validate_file(self, filename: str, file_size: int) -> Tuple[bool, str]:
        if file_size > self.max_file_size:
            return False, f"文件大小超过限制({self.max_file_size/1024/1024}MB)"
        _, ext = os.path.splitext(filename.lower())
        if ext not in self.SUPPORTED_EXTENSIONS:
            return (
                False,
                f"不支持的文件格式.支持的文件格式有:{','.join(self.SUPPORTED_EXTENSIONS)}",
            )
        return True, "文件验证通过"

    async def extract_Text_from_file(self, file_content: bytes, filename: str) -> str:
        try:
            mime_type = get_file_mime_type(filename)
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=os.path.splitext(filename)[1]
            ) as temp_file:
                temp_file.write(file_content)
                temp_path = temp_file.name
            try:
                extracted = await extract_text_content(temp_path, mime_type)
                return extracted
            finally:
                try:
                    os.unlink(temp_path)
                except Exception as e:
                    pass
        except Exception as e:
            logger.error(f"提取文件内容失败:{e}")
            raise Exception(f"文件解析失败:{e}")
async def extract_text_content(temp_path:str,mime_type:str)->str:
    