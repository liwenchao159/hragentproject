import json
import logging

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Form
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.database import get_db
from app.models import ResumeEvaluation
from app.models.user import User as UserSchema
from app.schemas.exam import ExamGenerateRequest, ExamSubmitRequest
from app.schemas.intent import RequirementParseRequest, RequirementParseResponse
from app.schemas.job_description import JDGenerateRequest
from app.schemas.resume_evaluation import ResumeEvaluationResult
from app.schemas.scoring_criteria import ScoringCriteriaGenerateRequest
from app.service.dify_service import DifyService
from app.service.job_description_service import JobDescriptionService
from app.service.resume_evaluation import ResumeEvaluationService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/parse-requirements")
async def parse_requirements(
        request: RequirementParseRequest,
        current_user: UserSchema = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
) -> Any:
    """
    将用户自然语言需求解析为结构化字段，供前端表单自动填充
    示例输入："JAVA开发工程师、3-5年工作经验、工作地点北京，薪资15000-20000"
    返回JSON字段：job_title, location, salary, experience, education, job_type, skills, benefits, department, additional_requirements
    """

    try:
        dify_service = DifyService()
        prompt = (
                "你是一个招聘助手。请从以下中文需求中提取结构化字段，并严格以JSON格式返回。\n"
                "不要添加解释，不要返回除JSON外的任何内容。\n"
                "需求文本：\n" + request.text + "\n\n"
                                               "JSON字段定义：{\n"
                                               "  \"job_title\": 岗位名称（如JAVA开发工程师、财务经理），\n"
                                               "  \"department\": 部门（如技术部、财务部，若无法判断可为空），\n"
                                               "  \"location\": 工作地点（城市名），\n"
                                               "  \"salary\": 薪资范围（原样返回，如15000-20000或25-35K），\n"
                                               "  \"experience\": 工作经验（如3-5年、5年以上），\n"
                                               "  \"education\": 学历要求（如本科、专科，若未提及可为空），\n"
                                               "  \"job_type\": 工作性质（如全职、兼职，若未提及可为空），\n"
                                               "  \"skills\": 技能标签数组（如[\"Java\", \"Spring\"]），\n"
                                               "  \"benefits\": 福利数组（如[\"五险一金\", \"带薪年假\"]），\n"
                                               "  \"additional_requirements\": 其他补充要求（原文提炼）。\n"
                                               "}\n"
                                               "示例返回：{\n"
                                               "  \"job_title\": \"JAVA开发工程师\",\n"
                                               "  \"department\": \"技术部\",\n"
                                               "  \"location\": \"北京\",\n"
                                               "  \"salary\": \"15000-20000\",\n"
                                               "  \"experience\": \"3-5年\",\n"
                                               "  \"education\": \"本科\",\n"
                                               "  \"job_type\": \"全职\",\n"
                                               "  \"skills\": [\"Java\", \"Spring\", \"MySQL\"],\n"
                                               "  \"benefits\": [\"五险一金\", \"带薪年假\"],\n"
                                               "  \"additional_requirements\": \"具备良好的沟通能力\"\n"
                                               "}"
        )
        ai_response = await dify_service.call_workflow_async(
            workflow_type=1,
            query=prompt,
            conversation_id=request.conversation_id,
            additional_inputs={"task": "parse_requirements"}
        )
        answer_text = ""
        if isinstance(ai_response, dict):
            if "answer" in ai_response:
                answer_text = ai_response["answer"]
            elif "data" in ai_response and isinstance(ai_response["data"], dict) and "answer" in ai_response["data"]:
                answer_text = ai_response["data"]["answer"]
            else:
                answer_text = json.dumps(ai_response, ensure_ascii=False)
        else:
            answer_text = str(ai_response)

        json_str = answer_text.strip()
        if "```" in json_str:
            if "```json" in json_str:
                start = json_str.find("```json") + 7
            else:
                start = json_str.find("```") + 3
            end = json_str.find("```", start)
            if end > start:
                json_str = json_str[start:end].strip()

        parsed: Dict[str, Any] = {}
        try:
            parsed = json.loads(json_str)
        except Exception:
            import re
            text = request.text
            parsed = {
                "job_title": None,
                "department": None,
                "location": None,
                "salary": None,
                "experience": None,
                "education": None,
                "job_type": None,
                "skills": [],
                "benefits": [],
                "additional_requirements": text
            }
            title_match = re.search(r"([A-Za-z]+开发工程师|[\u4e00-\u9fa5A-Za-z]+经理|[\u4e00-\u9fa5A-Za-z]+工程师)",
                                    text)
            if title_match:
                parsed["job_title"] = title_match.group(1)
            exp_match = re.search(r"(\d+\s*-\s*\d+年|\d+年以上)", text)
            if exp_match:
                parsed["experience"] = exp_match.group(1).replace(" ", "")
            loc_match = re.search(r"北京|上海|深圳|广州|杭州|南京|成都|重庆|苏州|武汉|西安", text)
            if loc_match:
                parsed["location"] = loc_match.group(0)
            sal_match = re.search(r"(\d+\s*-\s*\d+K|\d+\s*-\s*\d+|\d+K\s*-\s*\d+K)", text, re.IGNORECASE)
            if sal_match:
                parsed["salary"] = sal_match.group(1).replace(" ", "")
            edu_match = re.search(r"本科|专科|硕士|博士", text)
            if edu_match:
                parsed["education"] = edu_match.group(0)
            jobtype_match = re.search(r"全职|兼职|实习", text)
            if jobtype_match:
                parsed["job_type"] = jobtype_match.group(0)

        result = RequirementParseResponse(
            job_title=parsed.get("job_title"),
            department=parsed.get("department"),
            location=parsed.get("location"),
            salary=parsed.get("salary"),
            experience=parsed.get("experience"),
            education=parsed.get("education"),
            job_type=parsed.get("job_type"),
            skills=parsed.get("skills") or [],
            benefits=parsed.get("benefits") or [],
            additional_requirements=parsed.get("additional_requirements")
        )
        return result.model_dump()
    except Exception as e:
        logger.error(f"Error parsing requirements: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"解析需求失败: {str(e)}"
        )

    except Exception as e:
        logger.error(f"解析需求失败: {e}")
        raise HTTPException(status_code=500, detail="解析需求失败")


@router.post("/generate-jd")
async def generate_job_description(
        request: JDGenerateRequest,
        current_user: UserSchema = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """生成岗位JD（Job Description）
    工作流类型：type=1
    """
    try:
        dify_service = DifyService()
        query_parts = [f"请基于给定要求，生成岗位JD。要求如下:{request.requirements}"]
        if request.position_title:
            query_parts.append(f"岗位名称:{request.position_title}")
        if request.department:
            query_parts.append(f"部门：{request.department}")
        if request.experience_level:
            query_parts.append(f"经验要求：{request.experience_level}")

        query = "\n".join(query_parts)
        # 额外输入参数
        additional_inputs = {}
        if request.position_title:
            additional_inputs["position_title"] = request.position_title
        if request.department:
            additional_inputs["department"] = request.department
        if request.experience_level:
            additional_inputs["experience_level"] = request.experience_level

        if request.stream:
            # 流式响应
            async def generate_stream():
                async  for chunk in dify_service.call_workflow_stream(
                        workflow_type=1,
                        query=query,
                        conversation_id=request.conversation_id,
                        additional_inputs=additional_inputs
                ):
                    yield f"data: {chunk}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                generate_stream(),
                media_type="text/plain",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
            )
        else:
            result = await  dify_service.call_workflow_async(
                workflow_type=1,
                query=query,
                conversation_id=request.conversation_id,
                additional_inputs=additional_inputs
            )
            return result
    except Exception as e:
        logger.error(f"生成JD失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"生成JD失败: {str(e)}"
        )


@router.post("/generate-scoring-criteria")
async def generate_scoring_criteria(
        request: ScoringCriteriaGenerateRequest,
        current_user: UserSchema = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    """生成简历评分标准
    基于JD内容生成对应的简历评分标准
    工作流对应类型:type=2
    """

    try:
        dify_service = DifyService()
        query_parts = [
            f"""请基于以下JD内容，生成详细的简历评分标准：\n{request.jd_content}
请生成包含以下维度的评分标准：
1.技能匹配度（40%）
2.工作经验匹配（30%）
3.教育背景匹配度（15%）
4.项目经验匹配度（15%）
每个维度请提供具体的评分细则和分数区间
"""
        ]

        if request.job_title:
            query_parts.append(f"\n岗位名称：{request.job_title}")

        if request.requirements:
            if request.requirements.get('experience'):
                query_parts.append(f"经验要求：{request.requirements['experience']}")
            if request.requirements.get('education'):
                query_parts.append(f"学历要求：{request.requirements['education']}")
            if request.requirements.get('skills'):
                skills = request.requirements['skills']
                if isinstance(skills, list):
                    query_parts.append(f"技能要求：{', '.join(skills)}")
                else:
                    query_parts.append(f"技能要求：{skills}")
        query = "\n".join(query_parts)
        # 额外输入参数
        additional_inputs = {
            "jd_content": request.jd_content
        }
        if request.job_title:
            additional_inputs["job_title"] = request.job_title
        if request.requirements:
            additional_inputs["requirements"] = json.dumps(request.requirements, ensure_ascii=False)
        if request.stream:
            if request.stream:
                # 流式响应
                async def generate_stream():
                    async for chunk in dify_service.call_workflow_stream(
                            workflow_type=2,  # 使用type=2用于评分标准生成
                            query=query,
                            conversation_id=request.conversation_id,
                            additional_inputs=additional_inputs
                    ):
                        yield f"data: {chunk}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(
                    generate_stream(),
                    media_type="text/plain",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
                )
            else:
                # 同步响应
                result = await dify_service.call_workflow_sync(
                    workflow_type=2,
                    query=query,
                    conversation_id=request.conversation_id,
                    additional_inputs=additional_inputs
                )
                return result

    except Exception as e:
        logger.error(f"生成评分标准失败: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"生成评分标准失败: {str(e)}"
        )


@router.post("/evaluate", response_model=ResumeEvaluationResult)
async def evaluate_resume(
        file: UploadFile = File(..., description='简历文件'),
        job_description_id: str = Form(..., desscription='职位描述ID'),
        conversation_id: Optional[str] = Form(None, description="对话ID"),
        current_user: UserSchema = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
):
    try:
        jd_uuid, conv_uuid = await  ResumeEvaluationService.validate_evluation_params(
            job_description_id, conversation_id
        )
        # 读取文件内容
        file_content = await file.read()
        if not file_content:
            raise HTTPException(status_code=400, detail="文件内容为空")
        # 创建评价服务并执行评价
        evaluation_service = ResumeEvaluationService(db)
        result = await evaluation_service.evaluate_resume(
            user_id=current_user.id,
            file_content=file_content,
            filename=file.filename,
            job_description_id=jd_uuid,
            conversation_id=conv_uuid
        )

        return ResumeEvaluationResult(**result)

    except ValueError as e:
        logger.warning(f"简历评价参数错误: {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"简历评价失败: {e}")
        raise HTTPException(status_code=500, detail="简历评价服务暂时不可用")


@router.post("/generate-interview-plan-by-resume")
async def generate_interview_plan_by_resume(
        resume_id: str = Form(..., description="简历ID"),
        conversation_id: str = Form(None, description="对话ID"),
        stream: bool = Form(True, description="是否流式返回"),
        current_user: UserSchema = Depends(get_current_user),
        db: AsyncSession = Depends(get_db)
) -> Any:
    """
    根据简历ID生成面试方案
    工作流类型: type=4
    """
    try:
        # 查询简历评价记录
        result = await db.execute(
            select(ResumeEvaluation).where(
                ResumeEvaluation.id == resume_id,
                ResumeEvaluation.user_id == current_user.id
            )
        )
        resume_evaluation = result.scalar_one_or_none()

        if not resume_evaluation:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="简历记录未找到"
            )

        # 通过远程服务查询关联的JD
        jd_service = JobDescriptionService(db)
        try:
            job_description = await jd_service.get_job_description(
                jd_id=str(resume_evaluation.job_description_id),
                user_id=current_user.id
            )
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="关联的职位描述未找到"
            )
        except Exception as e:
            logger.error(f"获取职位描述失败: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=f"获取职位描述失败: {str(e)}"
            )

        # 构建简历内容
        jianli_content = resume_evaluation.resume_content

        dify_service = DifyService()

        # 构建查询内容
        query = f"请根据简历和JD要求生成面试方案。"

        # 额外输入参数
        additional_inputs = {
            "jianli": jianli_content,
            "jd": job_description.content
        }

        if stream:
            # 流式响应
            async def generate_stream():
                async for chunk in dify_service.call_workflow_stream(
                        workflow_type=4,
                        query=query,
                        conversation_id=conversation_id,
                        additional_inputs=additional_inputs
                ):
                    yield f"data: {chunk}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(
                generate_stream(),
                media_type="text/plain",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
            )
        else:
            # 同步响应
            result = await dify_service.call_workflow_async(
                workflow_type=4,
                query=query,
                conversation_id=conversation_id,
                additional_inputs=additional_inputs
            )
            return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error generating interview plan by resume: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"生成面试方案失败: {str(e)}"
        )


# 生成试卷
@router.post("/papers/generate")
async def generate_exam(
    request: ExamGenerateRequest,
    current_user: UserSchema = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
) -> Any:
    """
    基于文档内容生成试卷
    type=5
    """
    try:
        from app.service.exam_service import ExamService

        # 初始化试卷服务
        exam_service = ExamService(db)

        if request.stream:
            # 流式响应
            stream_generator = await exam_service.generate_exam(
                title=request.title,
                subject=request.subject,
                total_score=request.total_score,
                user_id=current_user.id,
                description=request.description,
                difficulty=request.difficulty,
                duration=request.duration,
                question_types=request.question_types,
                question_counts=request.question_counts,
                knowledge_files=request.knowledge_files,
                special_requirements=request.special_requirements,
                conversation_id=request.conversation_id,
                stream=True
            )

            return StreamingResponse(
                stream_generator,
                media_type="text/plain",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"}
            )
        else:
            # 同步响应
            result = await exam_service.generate_exam(
                title=request.title,
                subject=request.subject,
                total_score=request.total_score,
                user_id=current_user.id,
                description=request.description,
                difficulty=request.difficulty,
                duration=request.duration,
                question_types=request.question_types,
                question_counts=request.question_counts,
                knowledge_files=request.knowledge_files,
                special_requirements=request.special_requirements,
                conversation_id=request.conversation_id,
                stream=False
            )
            return result

    except Exception as e:
        logger.error(f"Error generating exam: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"生成试卷失败: {str(e)}"
        )


# 提交考试答案
@router.post("/papers/submit")
async def submit_exam(
        request: ExamSubmitRequest,
        db: AsyncSession = Depends(get_db)
):
    """
    提交考试答案并调用Dify进行自动评分
    type=6
    """
    try:
        from app.service.exam_service import ExamService

        # 初始化试卷服务
        exam_service = ExamService(db)

        # 提交考试答案
        result = await exam_service.submit_exam(
            exam_id=request.exam_id,
            student_name=request.student_name,
            department=request.department,
            answers=request.answers,
            exam_content=request.exam_content
        )

        return result

    except Exception as e:
        logger.error(f"Error submitting exam: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"提交考试失败: {str(e)}"
        )
