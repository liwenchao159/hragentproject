import logging
import re
from dataclasses import dataclass
from typing import Optional, List, Any, AsyncGenerator, Dict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Message
from app.models.conversation import MessageRole
from app.schemas.agent import AgentChatResponse, AgentStep
from app.service.intent_service import IntentService
from app.service.llm_service import LLMService

logger = logging.getLogger(__name__)



@dataclass(frozen=True)
class AgentToolSpec:
    """Agent 可规划调用的工具声明
        name 工具名是什么
        intent 对应哪个 intent
        route 对应哪个前端页面 route
        description 工具能做什么
        prerequisites 调用前需要满足哪些前置条件
    """

    name: str
    intent: str
    route: Optional[str]
    description: str
    prerequisites: List[str]


@dataclass(frozen=True)
class ReActDecision:
    """ReAct 单轮决策结果。
        thought 是给产品侧展示的简短推理摘要，不承载模型的完整隐藏推理过程。
    """

    mode: str
    intent: str
    action: str
    thought: str
    observation: str
    confidence: Optional[float] = None
    reply: Optional[str] = None
    source: str = "react"
def _normalize_memory_text(text: Optional[str]) -> Optional[str]:
    if not text:
        return ""
    normalized = re.sub(r"\s+", " ", text)
    return normalized


class AgentService:
    def __init__(self, db: AsyncSession):
        self.skill_dispatcher =self._build_skill_dispatcher()
        self.tool_registry = self._build_tool_registry()
        self.intent_service = IntentService(db)
        self.llm_service = LLMService()
        self.db = db

    async def stream_chat_agent(
            self,
            message: str,
            user_id: UUID,
            conversation_id: Optional[str] = None,
            auto_execute: bool = True,
            confirmed_requirements: Optional[Dict[str, Any]] = None,
            attachments: Optional[List[Dict[str, Any]]] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """流式处理普通 Agent 聊天：
            先推送思考状态，再返回最终规划/回复。
            """
        thinking_response = AgentChatResponse(
            message="我正在理解你的需求。",
            intent="thinking",
            route=None,
            steps=[
                AgentStep(
                    id="understand",
                    title="理解用户需求",
                    status="running",
                    detail="正在判断是否需要调用 HR 工具。",
                ),
                AgentStep(
                    id="plan",
                    title="选择下一步动作",
                    status="pending",
                    detail="识别完成后选择 chat、ask_user 或 use_tool。",
                ),
            ],
            suggestions=[],
        )
        yield {"type": "thinking", "response": thinking_response.model_dump()}

        # 附件
        normalized_attachments = self._normalize_attachments(attachments or [])
        # 读取对话记忆
        memory_context = await self._build_conversation_memory(conversation_id, user_id, message)

        agent_plan = None
        # 是否有用户确认后的结构化招聘需求
        if not confirmed_requirements:
            # 规划：下一步动作，轻量意图分类：优先规则和附件上下文，不为路由单独调用大模型
            agent_plan = self._build_rule_agent_plan(message, normalized_attachments, memory_context)
            if not agent_plan:
                # 规划：受控LLM ReAct决策
                agent_plan = await self._plan_agent_action(message, normalized_attachments, memory_context)

            # 常规聊天
            if agent_plan.get("mode") == "chat" or agent_plan.get("intent") == "general":
                chat_response = AgentChatResponse(
                    message="",
                    intent="general",
                    route=None,
                    steps=[
                        AgentStep(
                            id="understand",
                            title="完成需求判断",
                            status="completed",
                            detail=agent_plan.get("reason") or "判断为普通对话，不调用招聘工具。",
                        )
                    ],
                    suggestions=["生成 JD", "评分简历", "基于文档生成试卷"],
                )
                yield {"type": "plan", "response": chat_response.model_dump()}

                full_text = ""
                try:
                    if self.llm_service is None:
                        self.llm_service = LLMService()
                    chat_prompt = self._build_memory_augmented_prompt(message, memory_context)
                    async for delta in self.llm_service.stream_response(chat_prompt):
                        full_text += delta
                        yield {"type": "delta", "delta": delta}
                except Exception as exc:
                    logger.warning("Agent 普通聊天原生流式失败，使用规划回复兜底: %s", exc)
                    fallback_reply = self._clean_optional_value(agent_plan.get("reply")) or self._fallback_message(
                        "general", message)
                    async for delta in self._stream_text(fallback_reply):
                        full_text += delta
                        yield {"type": "delta", "delta": delta}

                final_response = chat_response.model_copy(
                    update={"message": full_text.strip() or self._fallback_message("general", message)})
                yield {"type": "final", "response": final_response.model_dump()}
                return

            # jd编辑
            if agent_plan.get("intent") == "jd_edit":
                async for event in self._stream_jd_edit_response(
                        message=message,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        memory_context=memory_context,
                ):
                    yield event
                return
            # 评分标准编辑
            if agent_plan.get("intent") == "criteria_edit":
                async for event in self._stream_criteria_edit_response(
                        message=message,
                        user_id=user_id,
                        conversation_id=conversation_id,
                        memory_context=memory_context,
                ):
                    yield event
                return
            # skill
            if agent_plan.get("intent") in self.skill_dispatcher.bundles:
                # 发送邮件
                if agent_plan.get("intent") == "email_notification":
                    async for event in self._stream_skill_draft_response(
                            intent="email_notification",
                            message=message,
                            user_id=user_id,
                            conversation_id=conversation_id,
                            route=self._route_for_intent("email_notification", message).get("route"),
                            memory_context=memory_context,
                            confirmed_requirements=confirmed_requirements,
                    ):
                        yield event
                    return

                response = await self.chat(
                    message=message,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    auto_execute=auto_execute,
                    confirmed_requirements=confirmed_requirements,
                    attachments=attachments,
                    agent_plan=agent_plan,
                )
                yield {
                    "type": "plan",
                    "response": response.model_copy(update={"message": ""}).model_dump(),
                }
                async for delta in self._stream_text(response.message):
                    yield {"type": "delta", "delta": delta}
                yield {"type": "final", "response": response.model_dump()}
                return

        # 根据 intent 构造响应
        response = await self.chat(
            message=message,
            user_id=user_id,
            conversation_id=conversation_id,
            auto_execute=auto_execute,
            confirmed_requirements=confirmed_requirements,
            attachments=attachments,
            agent_plan=agent_plan,
        )
        # 返回执行计划 steps 和 artifact阶段性产物
        yield {
            "type": "plan",
            "response": response.model_copy(update={"message": ""}).model_dump(),
        }
        async for delta in self._stream_text(response.message):
            # 流式文本片段
            yield {"type": "delta", "delta": delta}
        # 最终完整结果
        yield {"type": "final", "response": response.model_dump()}

    def _normalize_attachments(self, attachments: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """标准化附件的信息"""
        normalized = []
        for item in attachments:
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            normalized.append({
                "name": name,
                "size": item.get("size"),
                "content_type": item.get("content_type"),
                "extension": name.rsplit(".", 1)[-1].lower() if "." in name else "",
            })
        return normalized

    async def _build_conversation_memory(self,
                                         conversation_id: Optional[str],
                                         user_id: UUID,
                                         current_message: str,
                                         limit: int = 12,
                                         max_chars: int = 6000):
        """获取聊天会话上下文"""
        if not conversation_id:
            return ""
        try:
            conversation_uuid = UUID(str(conversation_id))
        except Exception as e:
            return ""
        try:
            conversation_result = await  self.db.execute(
                select(Conversation).where(Conversation.id == conversation_uuid))
            if not conversation_result.scalar_one_or_none():
                return ""
            result = await  self.db.execute(
                select(Message)
                .where(Message.conversation_id == conversation_uuid)
                .order_by(Message.created_at.desc()).limit(3))
            messages = list(reversed(result.scalars().all()))
        except Exception as e:
            logger.warning("读取 Agent 对话记忆失败%s", e)
            return ""
        normalized_current = _normalize_memory_text(current_message)
        memory_lines: List[str] = []
        for item in messages:
            content = _normalize_memory_text(item.content)
            if not content:
                continue
            if item.role == MessageRole.USER and normalized_current and content.startswith(normalized_current):
                continue
            role_label = "用户" if item.role == MessageRole.USER else ""
            if item.role == MessageRole.SYSTEM:
                role_label = "系统"
            memory_lines.append(f"{role_label}:{content[:900]}")
        memory = "\n".join(memory_lines)
        if len(memory) > max_chars:
            memory = memory[:max_chars]
        return memory.strip()

    def _build_rule_agent_plan(
            self,
            message: str,
            attachments: List[Dict[str, Any]],
            memory_context: str = "",
    ):
        if self._classify_agent_intent(message, attachments) == "resource_delete":
            return {
                "mode": "tool",
                "intent": "resource_delete",
                "reason": "用户明确要求删除招聘相关产物，调用删除资源工具。",
                "reply": None,
                "source": "delete_rule",
            }
        if self._is_criteria_edit_request(message) or self._is_criteria_edit_followup(message, memory_context):
            return {
                "mode": "tool",
                "intent": "criteria_edit",
                "reason": "用户要求修改评分标准，先定位最近评分标准并确认修改要求。",
                "reply": None,
                "source": "criteria_edit_rule",
            }
        if self._is_jd_edit_request(message) or self._is_jd_edit_followup(message, memory_context):
            return {
                "mode": "tool",
                "intent": "jd_edit",
                "reason": "用户要求修改已有 JD，先定位最近 JD 并确认修改要求。",
                "reply": None,
                "source": "jd_edit_rule",
            }
        rule_intent = self._classify_agent_intent(message, attachments)
        if rule_intent in {"jd", "resume_screening", "interview_plan", "exam_generate", "email_notification"}:
            return {
                "mode": "tool",
                "intent": rule_intent,
                "reason": "本地规则已明确识别到 HR 工具任务，优先进入对应业务链路。",
                "reply": None,
                "source": "rule_intent",
            }
        return None

    async def _plan_agent_action(self, message, normalized_attachments, memory_context):
        pass

    def _build_memory_augmented_prompt(self, message, memory_context):
        pass

    def _fallback_message(self, param, message):
        pass

    def _clean_optional_value(self, param):
        pass

    def _stream_text(self, fallback_reply):
        pass

    def _stream_jd_edit_response(self, message, user_id, conversation_id, memory_context):
        pass

    async def chat(self, message, user_id, conversation_id, auto_execute, confirmed_requirements, attachments,
                   agent_plan):
        pass

    def _stream_criteria_edit_response(self, message, user_id, conversation_id, memory_context):
        pass

    def _classify_agent_intent(
            self,
            message: str,
            attachments: List[Dict[str, Any]]) -> str:
        """轻量意图分类：优先规则和附件上下文，不为路由单独调用大模型。"""
        lowered=message.lower()
        if re.search(r'删除|移除|去掉|清理',message) and re.search(r"简历|jd|岗位|候选人|面试|试卷|考试",lowered):
            return "resource_delete"

        if self._is_criteria_edit_request(message):
            return "criteria_edit"
        if self._is_jd_edit_request(message):
            return "jd_edit"
        
        fast_intent = self.intent_service.classify_intent_fast(message)
        if fast_intent in self.tool_registry:
            return fast_intent


    def _is_criteria_edit_request(self, message: str) -> bool:
        has_target = bool(re.search(r"评分标准|评分规则|打分标准|筛选标准|简历评分标准|简历评分规则", message))
        has_edit_action = bool(re.search(r"改改|修改|调整|优化|更新|编辑|润色|改成|改为|换成|加上|增加|删掉|删除|去掉|降低|提高", message))
        return has_target and has_edit_action

    def _is_jd_edit_request(self, message: str) -> bool:
        lowered = message.lower()
        has_jd_target = bool(re.search(r"jd|职位描述|岗位说明书|招聘需求|职位|岗位", lowered))
        if (
            has_jd_target
            and re.search(r"删除|删掉|移除|清理", message)
            and not re.search(r"把|将|内容|职责|要求|福利|技能|薪资|地点|经验|学历", message)
        ):
            return False
        has_edit_action = bool(re.search(r"改改|修改|调整|优化|更新|编辑|润色|改成|改为|换成|加上|增加|删掉|删除|去掉", message))
        has_context = bool(re.search(r"上次|刚才|刚刚|这个|这份|这条|那个|上一版|最近|最新|原来", message))
        return has_jd_target and has_edit_action and (has_context or "jd" in lowered or "职位描述" in message or "岗位说明书" in message)

    def _build_tool_registry(self):
       """声明Agent能自主规划的工具和前置条件"""
       tools=[
           AgentToolSpec(
               name="generate_jd",
               intent="jd",
               route="/recruitment/jd-generator",
               description="生成岗位 JD，并自动生成简历评分标准",
               prerequisites=["确认岗位名称、地点、薪资、经验、学历"],
           ),
           AgentToolSpec(
                name="edit_jd",
                intent="jd_edit",
                route="/recruitment/jd-generator",
                description="修改最近生成或保存的岗位 JD",
                prerequisites=["定位要修改的 JD", "明确修改要求"],
            ),
            AgentToolSpec(
                name="edit_scoring_criteria",
                intent="criteria_edit",
                route="/recruitment/resume-screening",
                description="修改最近生成或保存的简历评分标准",
                prerequisites=["定位要修改的评分标准", "明确修改要求"],
            ),
            AgentToolSpec(
                name="evaluate_resume",
                intent="resume_screening",
                route="/recruitment/resume-screening",
                description="基于 JD 批量评分简历",
                prerequisites=["上传 PDF/DOC/DOCX 简历", "选择用于匹配的 JD"],
            ),
            AgentToolSpec(
                name="generate_interview_plan",
                intent="interview_plan",
                route="/recruitment/smart-interview",
                description="基于已评分简历和 JD 生成面试计划",
                prerequisites=["选择一位已评分候选人"],
            ),
            AgentToolSpec(
                name="generate_exam",
                intent="exam_generate",
                route="/training/exam-generator",
                description="基于上传文档生成考试试卷",
                prerequisites=["上传参考文档", "确认试卷配置"],
            ),
            AgentToolSpec(
                name="delete_resource",
                intent="resource_delete",
                route=None,
                description="按用户描述删除已生成的 JD、简历评分记录、面试方案或试卷",
                prerequisites=["明确要删除的资源类型和名称/候选人/标题"],
            )
       ]
       registry = {tool.intent: tool for tool in tools}
       for bundle in self.skill_dispatcher.bundles.values():

    def _build_skill_dispatcher(self):
        return build_default_skill_dispatcher()


















































