import hashlib
import mimetypes
import os
import tempfile
from typing import Tuple
import logging

from app.utils.file_utils import get_file_mime_type
from app.utils.text_utils import extract_text_content

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

    """获取文件的基本信息

    """

    def get_file_info(self, filename: str, file_content: bytes) -> dict:
        """获取文件的基本信息"""
        file_size = len(file_content)
        file_hash = hashlib.sha256(file_content).hexdigest()
        _, ext = os.path.splitext(filename)
        return {
            "filename": filename,
            "file_type": ext.lstrip("."),
            "file_size": file_size,
            "file_hash": file_hash,
            "mime_type": mimetypes.guess_type(filename)[0],
        }

    async def extract_text_from_file(self, file_content: bytes, filename: str) -> str:
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

    """从上下文中提取文本"""

    async def _extract_from_txt(self, file_content: bytes) -> str:
        try:
            encodings = ["utf-8", "utf-16", "gbk", "gb2312", "big5", "latin-1"]
            for encoding in encodings:
                try:
                    return file_content.decode(encoding)
                except UnicodeDecodeError:
                    continue
            return file_content.decode("utf-8", errors="ignore")
        except Exception as e:
            logger.error(f"txt文件解析失败:{e}")
            raise

    """
    从PDF文件提取文本
    """

    async def _extract_from_pdf(self, file_content: bytes) -> str:
        try:
            import PyPDF2
            import io

            pdf_reader = PyPDF2.PdfReader(io.BytesIO(file_content))
            text_content = []
            for page in pdf_reader.pages:
                page_text = page.extract_text()
                if page_text:
                    text_content.append(page_text)
            return "\n".join(text_content)
        except ImportError:
            try:
                import pdfplumber
                import io

                with pdfplumber.open(io.BytesIO(file_content)) as pdf:
                    text_content = []
                    for page in pdf.pages:
                        text = page.extract_text()
                        if text:
                            text_content.append(text)
                    return "\n".join(text_content)
            except ImportError:
                logger.error("PDF解析库未安装，请安装 PyPDF2 或 pdfplumber")
                raise Exception("PDF解析功能不可用，请联系管理员")
        except Exception as e:
            logger.error(f"PDF文件解析失败: {e}")
            raise Exception(f"PDF文件解析失败: {str(e)}")
    """从doc文件中提取文件"""
    async def _extract_from_doc(self,file_content: bytes)->str:
        try:
            with tempfile.NamedTemporaryFile(
                delete=False, suffix="doc"
            ) as temp_file:
                temp_file.write(file_content)
                temp_file_path=temp_file.name
            try:
                try:
                    import textract
                    text_bytes=textract.process(temp_file_path)
                    decoded=None
                    for enc in ['utf-8', 'utf-16', 'gbk', 'gb2312', 'big5', 'latin-1']:
                        try:
                            decoded = text_bytes.decode(enc)
                            break
                        except Exception:
                            continue
                    if decoded is None:
                        decoded = text_bytes.decode('utf-8', errors='ignore')
                    text = decoded.strip()
                    if text:
                        return text                   
            except ImportError:
                logger.warning("未安装textract，尝试其他方式解析doc")
            except Exception as e:
                logger.warning(f"textract解析doc失败:{e}")
                
            try:
                
                
        except Exception as e:
            