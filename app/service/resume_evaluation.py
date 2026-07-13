import json
import logging
import os.path
import pathlib
import re
from typing import Any, Dict, Optional, List
from uuid import UUID

import aiofiles
import httpx
from dns.e164 import query
from sqlalchemy import select, func

from app.core.config import settings
from app.models.resume_evaluation import ResumeStatus, ResumeEvaluation
from app.schemas.job_description import JobDescriptionResponse
from app.schemas.resume_evaluation import AIEvaluationResult, EvaluationMetric, ResumeEvaluationResponse, \
    ResumeEvaluationListResponse
from app.service.dify_service import DifyService
from app.service.llm_service import LLMService
from app.service.remote_service_client import remote_service_client
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
            # 4. 获取JD信息（使用JD所属用户ID查询）
            _jd_uid = jd_user_id or user_id
            jd = await self._get_job_description(job_description_id, _jd_uid)
            if not jd:
                raise ValueError("职位描述不存在")
            # 5. 获取评价模型
            evaluation_model = await self._get_evaluation_model(job_description_id)

            # 6. 调用Dify API进行评价
            ai_result, raw_response = await self._call_dify_evaluation(
                resume_text=resume_text,
                evaluation_model=evaluation_model,
                jd_info=jd
            )
            # 7. 保存上传的文件到磁盘
            saved_file_path = await self._save_uploaded_file_content(filename, file_content, user_id)
            logger.info(f"文件已保存到: {saved_file_path}")
            # 更新file_info以包含保存的文件路径
            file_info['file_path'] = saved_file_path

            # 8. 保存评价结果
            evaluation_record = await self._save_evaluation_result(
                user_id=user_id,
                created_by=user_id,
                file_info=file_info,
                resume_text=resume_text,
                ai_result=ai_result,
                job_description_id=job_description_id,
                conversation_id=conversation_id,
                email_id=email_id,
                raw_response=raw_response
            )

            logger.info(f"评价记录已保存，ID: {evaluation_record.id}, 文件路径: {evaluation_record.file_path}")

            # 8. 返回完整结果
            return {
                "id": evaluation_record.id,
                "evaluation_metrics": [metric.model_dump() for metric in ai_result.evaluation_metrics],
                "total_score": ai_result.total_score,
                "name": ai_result.name,
                "position": ai_result.position,
                "workYears": (self._parse_work_years_to_float(ai_result.workYears) or 0.0),
                "education": ai_result.教育水平,
                "age": ai_result.年龄,
                "sex": ai_result.sex,
                "school": ai_result.school,
                "resume_content": resume_text,
                "original_filename": file_info['filename'],
                "created_at": evaluation_record.created_at.isoformat(),
                "updated_at": evaluation_record.updated_at.isoformat()
            }
        except Exception as e:
            logger.error(f"简历评价失败: {e}")
            raise
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

    async def _get_job_description(self, jd_id: UUID, user_id: UUID) -> Optional[JobDescriptionResponse]:
        """获取职位描述 - 使用远程服务"""
        try:
            result_data = await remote_service_client.get(
                endpoint=f"job-descriptions/{jd_id}",
                user_id=user_id
            )
            return JobDescriptionResponse(**result_data)
        except Exception as e:
            logger.error(f"获取职位描述失败: {e}")
            return None

    async def _get_evaluation_model(self, jd_id: UUID) -> str:
        """获取评价模型"""
        try:
            # 从远程服务获取与JD关联的评分标准
            # 注意：该接口不需要current_user_id参数，直接使用httpx调用
            url = f"{remote_service_client.base_url}/scoring-criteria/by-jd/{jd_id}"
            headers = remote_service_client._get_headers()

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=headers,
                    timeout=remote_service_client.timeout
                )

            result_data = remote_service_client._handler_response(response)

            # 检查返回数据中是否有content字段
            if result_data and result_data.get('content'):
                return result_data['content']

            # 如果没有找到特定的评价模型，返回默认模型
            return self._get_default_evaluation_model()

        except Exception as e:
            logger.error(f"获取评价模型失败: {e}")
            return self._get_default_evaluation_model()

    def _get_default_evaluation_model(self):
        """获取默认评价模型"""
        return """
               请根据以下职位要求对简历进行评价，并按照指定的JSON格式返回结果：

               评价维度：
               1. 学历匹配度 (0-20分)
               2. 工作经验匹配度 (0-25分)
               3. 技能匹配度 (0-25分)
               4. 项目经验匹配度 (0-20分)
               5. 综合素质 (0-10分)

               请提取简历中的以下信息：
               - 姓名
               - 应聘岗位
               - 工作年限
               - 教育水平
               - 年龄
               - 性别
               - 毕业院校

               返回格式必须是有效的JSON：
               {
                 "evaluation_metrics": [
                   {
                     "name": "学历",
                     "score": 15,
                     "max": 20,
                     "reason": "本科学历，符合岗位要求"
                   }
                 ],
                 "total_score": 85,
                 "name": "张三",
                 "position": "前端开发工程师",
                 "workYears": "3年",
                 "education": "本科",
                 "age": 28,
                 "sex": "男",
                 "school": "上海理工大学"
               }
               """

    async def _call_dify_evaluation(self, resume_text: str, evaluation_model: str, jd_info: JobDescriptionResponse) ->tuple[AIEvaluationResult, str]:
        """调用dify api进行简历评价"""
        try:
            response=await self.dify_service.call_workflow_async(
                workflow_type=3,
                query=evaluation_model,
                additional_inputs={
                    "jianli":resume_text,
                    "jobName":jd_info.title
                }
            )
            ai_result=self._parse_ai_response(response)
            raw_response=str(response)
            return ai_result,raw_response
        except Exception as e:
            logger.error(f"调用dify api失败: {e}")
            raise Exception(f"AI评价服务暂时不可用:str{e}")

    def _parse_ai_response(self, response: Dict[str, Any]) -> AIEvaluationResult:
        """解析AI响应，提取评价结果"""
        try:
            # 从Dify响应中提取答案文本
            answer_text = ""
            if "answer" in response:
                answer_text = response["answer"]
            elif "data" in response and "answer" in response["data"]:
                answer_text = response["data"]["answer"]
            else:
                # 如果没有找到answer字段，尝试从其他可能的字段获取
                answer_text = str(response)

            if not answer_text:
                raise ValueError("AI响应为空")

            # 尝试解析JSON
            try:
                # 尝试直接解析JSON
                if answer_text.startswith('{') and answer_text.endswith('}'):
                        result_data = json.loads(answer_text)
                else:

                    if '```json' in answer_text:
                        start = answer_text.find('```json') + 7
                        end = answer_text.find('```', start)
                        json_str = answer_text[start:end].strip()
                    elif '```' in answer_text:
                        start = answer_text.find('```') + 3
                        end = answer_text.find('```', start)
                        json_str = answer_text[start:end].strip()
                    else:
                        # 如果不是纯JSON，尝试提取JSON部分
                        json_start = answer_text.find('{')
                        json_end = answer_text.rfind('}') + 1
                        if json_start != -1 and json_end > json_start:
                            json_str = answer_text[json_start:json_end]
                        else:
                            raise ValueError("No valid JSON found in response")

                    # 解析JSON
                    result_data = json.loads(json_str)
                # 验证必要字段
                if 'evaluation_metrics' not in result_data:
                    raise ValueError("缺少evaluation_metrics字段")

                if 'total_score' not in result_data:
                    raise ValueError("缺少total_score字段")

                # 构建评价指标列表
                metrics = []
                for metric_data in result_data.get('evaluation_metrics', []):
                    metric = EvaluationMetric(
                        name=metric_data.get('评价指标', ''),
                        score=metric_data.get('score', 0),
                        max=metric_data.get('max', 100),
                        reason=metric_data.get('reason', '')
                    )
                    metrics.append(metric)
                    # 规范化字段别名，兼容不同返回命名
                normalized_work_years = (
                        result_data.get('workYears')
                        or result_data.get('work_years')
                        or result_data.get('work_experience')
                        or result_data.get('工作年限')
                        or result_data.get('工作经验')
                )
                # 处理workYears字段，确保能正确转换为float
                try:
                    if normalized_work_years is not None and normalized_work_years != '' and normalized_work_years != '未知':
                        normalized_work_years = float(normalized_work_years)
                    else:
                        normalized_work_years = 0.0
                except (ValueError, TypeError):
                    normalized_work_years = 0.0
                normalized_education = (
                        result_data.get('education')
                        or result_data.get('education_level')
                        or result_data.get('学历')
                        or result_data.get('教育水平')
                )
                try:
                    normalized_age = int(result_data.get('age', result_data.get('年龄', 0)))
                except (ValueError, TypeError):
                    normalized_age = 0

                normalized_sex = (
                        result_data.get('sex')
                        or result_data.get('gender')
                        or result_data.get('性别')
                )
                normalized_school = (
                        result_data.get('school')
                        or result_data.get('毕业院校')
                        or result_data.get('院校')
                        or result_data.get('学校')
                )

                # 构建AI评价结果
                ai_result = AIEvaluationResult(
                    evaluation_metrics=metrics,
                    total_score=result_data.get('total_score', 0),
                    name=result_data.get('name', ''),
                    position=result_data.get('position', ''),
                    workYears=normalized_work_years,
                    教育水平=normalized_education or '',
                    年龄=normalized_age,
                    sex=normalized_sex or '',
                    school=normalized_school or ''
                )

                return ai_result

            except json.JSONDecodeError as e:
                logger.error(f"JSON解析失败: {e}, 原始响应: {answer_text}")
                # 返回默认结果
                return self._create_default_result(answer_text)

        except Exception as e:
            logger.error(f"解析AI响应失败: {e}")
            return self._create_default_result(str(e))

    async def _save_uploaded_file_content(self, filename:str, file_content:bytes, user_id:UUID)->str:
        try:
            upload_dir=os.path.join(settings.UPLOAD_DIR,str(user_id))
            os.mkdir(upload_dir, exist_ok=True)
            safe_filename=pathlib.Path(filename).name
            file_path=os.path.join(upload_dir,safe_filename)
            counter=1
            base_name,extension=os.path.splitext(safe_filename)
            original_file_path=file_path
            while os.path.exists(file_path):
                new_file=f"{base_name}_{counter}{extension}"
                file_path=os.path.join(upload_dir,new_file)
                counter+=1
            async with aiofiles.open(file_path,mode="wb") as f:
                await f.write(file_content)
            return file_path
        except Exception as e:
            logger.error(f"保存上传文件内容失败: {e}")

    async def _save_evaluation_result(
            self,
            user_id: UUID,
            created_by: UUID,
            file_info: Dict[str, Any],
            resume_text: str,
            ai_result: AIEvaluationResult,
            job_description_id: UUID,
            raw_response: str = "",
            email_id: Optional[str] = None,
            conversation_id: Optional[UUID] = None
    ) -> ResumeEvaluation:
        """保存评价结果 - 从参数获取已保存的文件路径"""
        try:
            # 直接创建ResumeEvaluation对象（文件已经保存）
            evaluation = ResumeEvaluation(
                user_id=user_id,
                created_by=created_by,
                updated_by=created_by,
                email_id=email_id,
                original_filename=file_info['filename'],
                file_path=file_info.get('file_path'),  # 使用传入的文件路径
                file_type=file_info['file_type'],
                file_size=file_info['file_size'],
                resume_content=resume_text,
                candidate_name=ai_result.name,
                candidate_position=ai_result.position,
                candidate_age=ai_result.年龄,
                candidate_gender=ai_result.sex,
                work_years=(self._parse_work_years_to_float(ai_result.workYears) or 0.0),
                education_level=ai_result.教育水平,
                school=ai_result.school,
                total_score=ai_result.total_score,
                evaluation_metrics=[metric.model_dump() for metric in ai_result.evaluation_metrics],
                job_description_id=job_description_id,
                conversation_id=str(conversation_id) if conversation_id else None,
                ai_response=raw_response
            )
            self.db.add(evaluation)
            await self.db.commit()
            await self.db.refresh(evaluation)

            return evaluation

        except Exception as e:
            await self.db.rollback()
            logger.error(f"保存评价结果失败: {e}")
            raise

    def _parse_work_years_to_float(self, text: Optional[str]) -> Optional[float]:
        """将工作年限字符串解析为数字（年）。支持格式如"3年"、"1.5年"、"1-3年"、"约2年"。
        - 解析到范围时取平均值；
        - 仅提取到一个数值时使用该数值；
        - 解析失败返回None。
        """
        if not text:
            return None
        s = str(text).strip().lower()
        # 常见非数值占位统一视为0（回退到调用处做 or 0.0）
        if s in {"未知", "不详", "none", "null", "n/a", "na", "--", "-", "", "应届", "应届生", "fresh"}:
            return 0.0
        # 匹配范围 "a-b" 或 "a – b"
        m_range = re.search(r"(\d+(?:\.\d+)?)\s*[\-~–—]\s*(\d+(?:\.\d+)?)", s)
        if m_range:
            try:
                a = float(m_range.group(1))
                b = float(m_range.group(2))
                return (a + b) / 2.0
            except Exception:
                pass
        # 提取第一个数字
        m_single = re.search(r"(\d+(?:\.\d+)?)", s)
        if m_single:
            try:
                return float(m_single.group(1))
            except Exception:
                return None
        return None

    async def get_evaluation_history_with_pagination(self, user_id, skip, limit, status):
        """获取评价历史并返回分页响应"""
        evaluations, total = await self.get_evaluation_history(
            user_id=user_id,
            skip=skip,
            limit=limit,
            status=status
        )
        # 转换为响应格式
        evaluation_responses = []
        for evaluation in evaluations:
            response = ResumeEvaluationResponse(
                id=evaluation.id,
                original_filename=evaluation.original_filename,
                file_type=evaluation.file_type,
                resume_content=evaluation.resume_content,
                candidate_name=evaluation.candidate_name,
                candidate_position=evaluation.candidate_position,
                candidate_age=evaluation.candidate_age,
                candidate_gender=evaluation.candidate_gender,
                work_years=evaluation.work_years,
                education_level=evaluation.education_level,
                school=evaluation.school,
                total_score=evaluation.total_score,
                evaluation_metrics=evaluation.evaluation_metrics,
                job_description_id=evaluation.job_description_id,
                scoring_criteria_id=evaluation.scoring_criteria_id,
                user_id=evaluation.user_id,
                created_at=evaluation.created_at,
                updated_at=evaluation.updated_at
            )
            evaluation_responses.append(response)
        # 计算分页信息
        page = (skip // limit) + 1 if limit > 0 else 1
        pages = (total + limit - 1) // limit if limit > 0 else 1
        return ResumeEvaluationListResponse(
            items=evaluation_responses,
            total=total,
            page=page,
            size=limit,
            pages=pages
        )

    async  def get_evaluation_history(self,user_id:UUID,skip:int=0,limit:int=20,status:Optional[str]=None)->tuple[List[ResumeEvaluation],int]:
        """获取评价历史总记录"""
        try:
            query=select(ResumeEvaluation).where(ResumeEvaluation.user_id==user_id).order_by(ResumeEvaluation.created_at.desc())
            if status:
                query=query.where(ResumeEvaluation.status==status)

            count_query=select(func.count()).select_from(query.subquery())
            count_result=await self.db.execute(count_query)
            total=count_result.scalar()
            query=query.offset(skip).limit(limit)
            result=await self.db.execute(query)
            evaluations=result.scalars().all()
            logger.info(f"查询到{total}条评价记录")
            if evaluations:
                logger.info(f"第一个评价记录类型: {type(evaluations[0])}")
                if hasattr(evaluations[0], 'id'):
                    logger.info(f"第一个评价记录ID: {evaluations[0].id}")
                else:
                    logger.info("第一个评价记录没有id属性")
            return evaluations,total
        except Exception as e:
            logger.error(f"获取评价历史失败: {e}")
            return [],0