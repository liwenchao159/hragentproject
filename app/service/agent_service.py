import json
import logging
import re
from dataclasses import dataclass
from typing import Optional, List, Any, AsyncGenerator, Dict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation, Message
from app.models.conversation import MessageRole
from app.schemas.agent import AgentChatResponse, AgentStep, AgentArtifact
from app.service.agent_skill import build_default_skill_dispatcher
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
    normalized = re.sub(r"s+", " ", text)
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
        memory = "n".join(memory_lines)
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

    async def _plan_agent_action(
            self,
            messages:str,
            attachments:List[Dict[str, Any]],
            memory_context:str=""
    )->Dict[str, Any]:
        """使用受控ReAct決策選擇下一步動作

        這裏的React是有限動作的空間版本：模型只有在chat /use_tool /ask_user
        中選擇，并且use_tool只能選擇tool_registry 聲明過的業務工具
        """

        attachment_text="无"
        if attachments:
            attachment_text="、".join(f"{item.get('name')}({item.get('extension') or '未知格式'})" for item in attachments)
        tool_text="n".join(
            f"-{tool.intent}:{tool.name},{tool.description}:前置条件：{'、'.join(tool.prerequisites)}" for tool in self.tool_registry.values()
        )
        allowed_intents="、".join(self._allowed_intents())
        prompt=f"""
        你是HR Agent的ReAct控制器。你哟啊按照Thought->Action->Observation的方式，
        为当前用户消息选择下一步动作。
        请只输出一个JSON对象，不要输出Markdown，不要解释JSON之外的内容。
        可用工具:
           {tool_text}
        动作空间：
         - chat:普通对话、打招呼、解释能力边界，不用该调用工具
         - use_tool:用户明确要执行的HR任务，选择一个工具Intent
         - ask_user:用户目标明确但缺少关键前置条件，选择对应工具intent，并由业务链追问
         
        决策规则：
          1.不确定时优先chat，避免误触发工具
          2.生成JD、修改JD、修改评分标准、简历筛选/评分、面试计划、试卷生成、邮件草稿应该选择对应工具
          3.缺少附件、JD、候选人、考试配置等前置条件时，选择ask_user 或 use_tool，后续工具链会生成表单/选择器
          4.邮件只能生成草稿，不允许自动发送。
          5.intent只能选择这些值之一：{allowed_intents}。
          6.用户要求删除JD、建立记录、面试方案、试卷时，选择 resource_delete
          7.用户收’改改上次那个JD、修改刚才的职位描述、把这个JD的薪资改成...时‘选择 jd_edit
          8.用户说“修改评分标准、调整简历评分规则、把技能匹配改成40分”时，选择 criteria_edit。
          9.thought只写一句简短推理摘要,不要展开内部推理
          10.用户说“继续、这个、刚才、删一个、按前面”等指代时，优先结合历史对话记忆理解
        返回json字段：
          {{
            "thought": "一句推理摘要",
            "action": "chat/use_tool/ask_user",
            "intent": "...",
            "tool":"工具名或 null",
            "action_input": {{}},
            "observation": "预期观察或已知前置条件",
            "confidence": 0.0,
            "reply": "action=chat 时的自然回复，否则为 null"
            }}
        {self._format_memory_for_prompt(memory_context)}
        用户消息：{messages},
        附件：{attachment_text}
        
        """
        try:
            if self.llm_service is None:
                self.llm_service = LLMService()
            response = await self.llm_service.generate_response(prompt)
            planned = self._safe_json_loads(response)
            action = str(planned.get("action") or planned.get("mode") or "").lower()
            intent = str(planned.get("intent") or "").strip()

            if action == "use_tool":
                mode = "tool"
            elif action == "ask_user":
                mode = "tool"
            else:
                mode = "chat"
                action = "chat"
            if mode in {"chat", "tool"} and intent in self._allowed_intents():
                decision = ReActDecision(
                    mode=mode,
                    intent=intent,
                    action=action,
                    thought=self._clean_optional_value(planned.get("thought"))
                            or self._clean_optional_value(planned.get("reason"))
                            or "已完成 ReAct 决策。",
                    observation=self._clean_optional_value(planned.get("observation"))
                                or "等待执行动作后观察结果。",
                    confidence=planned.get("confidence"),
                    reply=planned.get("reply"),
                    source="react_llm",
                )
                return self._decision_to_plan(decision)
        except Exception as exc:
            logger.warning("Agent ReAct 决策失败，使用规则兜底: %s", exc)
        fallback_intent = self._classify_agent_intent(messages, attachments)
        fallback_action = "use_tool" if fallback_intent in self.tool_registry else "chat"
        decision = ReActDecision(
            mode="tool" if fallback_intent in self.tool_registry else "chat",
            intent=fallback_intent,
            action=fallback_action,
            thought="ReAct 决策失败后，使用本地关键词和附件规则选择下一步。",
            observation="本地规则已给出可执行意图。" if fallback_action == "use_tool" else "未匹配到需要调用的工具。",
            source="fallback",
        )
        return self._decision_to_plan(decision)


    def _build_memory_augmented_prompt(self, message: str, memory_context: str) -> str:
        if not memory_context:
            return message
        return (
            "请结合以下历史对话记忆回答当前用户消息。"
            "如果用户使用“继续、这个、刚才、上一个”等指代，请从历史中解析；"
            "如果历史无关，不要强行引用。nn"
            f"{self._format_memory_for_prompt(memory_context)}"
            f"当前用户消息：{message}"
        )

    def _fallback_message(self, param, message):
        pass

    def _clean_optional_value(self, value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.lower() in {"none", "null", "n/a", "na"}:
            return None
        if text in {"无", "暂无", "未提及", "未说明", "未知"}:
            return None
        return text

    def _stream_text(self, fallback_reply):
        pass

    async def _stream_jd_edit_response(
        self,
        message: str,
        user_id: UUID,
        conversation_id: Optional[str],
        memory_context: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        selected_tool = self.tool_registry.get("jd_edit")
        edit_request = await self._parse_jd_edit_request(message, memory_context)
        steps = [
            self._planning_step(selected_tool, "识别到 JD 修改请求，先定位目标 JD。"),
            AgentStep(id="locate_jd", title="定位要修改的 JD", status="running", detail="优先查找当前对话最近生成的 JD。", tool="edit_jd"),
            AgentStep(id="collect_changes", title="确认修改要求", status="pending", detail="拿到明确修改点后再改写。"),
            AgentStep(id="rewrite_jd", title="改写 JD 内容", status="pending", detail="基于原 JD 和修改要求生成新版。"),
            AgentStep(id="save_jd", title="保存修改", status="pending", detail="更新原 JD 记录。"),
        ]
        artifacts: List[AgentArtifact] = []
        yield self._jd_edit_stream_event("plan", "正在定位要修改的 JD。", steps, artifacts)
        candidates = await self._resolve_recent_jd_candidates(user_id, conversation_id, edit_request.get("keyword") or "")

    async def chat(
        self,
        message: str,
        user_id: UUID,
        conversation_id: Optional[str] = None,
        auto_execute: bool = True,
        confirmed_requirements: Optional[Dict[str, Any]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        agent_plan: Optional[Dict[str, Any]] = None,
    ) -> AgentChatResponse:
        """处理用户消息并执行招聘 Agent 任务"""
        normalized_attachments = self._normalize_attachments(attachments or [])
        memory_context = await self._build_conversation_memory(conversation_id, user_id, message)
        if confirmed_requirements:
            confirmation_action = str(confirmed_requirements.get("action") or "").strip()
            confirmed_skill = self.skill_dispatcher.match_confirmation_action(
                confirmation_action) if confirmation_action else None
            if confirmed_skill:
                intent = confirmed_skill.intent
                agent_plan = {
                    "mode": "tool",
                    "intent": confirmed_skill.intent,
                    "reason": f"用户已确认 {confirmed_skill.bundle_name} 所需信息，继续执行 skill。",
                    "reply": None,
                    "source": "confirmed_skill_action",
                }
            else:
                intent = "jd"
                agent_plan = {
                    "mode": "tool",
                    "intent": "jd",
                    "reason": "用户已确认 JD 生成信息，继续执行 JD 工具链。",
                    "reply": None,
                    "source": "confirmed_requirements",
                }
        else:
            agent_plan = agent_plan or self._build_rule_agent_plan(message, normalized_attachments, memory_context)
            agent_plan = agent_plan or await self._plan_agent_action(message, normalized_attachments, memory_context)
            intent = agent_plan["intent"]
        route_result = self._route_for_intent(intent, message)
        selected_tool = self.tool_registry.get(intent)
        if agent_plan["mode"] == "chat" or intent == "general":
            reply = self._clean_optional_value(agent_plan.get("reply")) or self._fallback_message("general", message)
            return AgentChatResponse(
                message=reply,
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
        if intent == "resource_delete":
            return await self._handle_resource_delete(message, user_id, selected_tool, conversation_id)


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
                registry[bundle.intent] =AgentToolSpec(
                    name=bundle.metadata.name,
                    intent=bundle.intent,
                    route=bundle.metadata.route,
                    description=bundle.metadata.description or f"執行{bundle.bundle_name} Skill",
                    prerequisites=bundle.metadata.prerequisites,
                ) 
        return registry
    def _build_skill_dispatcher(self):
        return build_default_skill_dispatcher()

    def _is_criteria_edit_followup(self, message: str, memory_context: str = "") -> bool:
        if not memory_context:
            return False
        waiting_for_changes = bool(re.search(r"想具体改哪些评分规则|等待用户说明要修改哪些评分规则|等待修改要求", memory_context))
        if not waiting_for_changes:
            return False
        return bool(re.search(r"改成|改为|换成|加上|增加|删掉|删除|去掉|降低|提高|分|权重|维度|技能|经验|学历|项目|加分|淘汰", message, re.I))

    def _is_jd_edit_followup(self, message: str, memory_context: str = "") -> bool:
        if not memory_context:
            return False
        waiting_for_changes = bool(re.search(r"你想具体改哪些内容|等待用户说明要修改哪些内容|等待修改要求|想具体改哪些内容", memory_context))
        if not waiting_for_changes:
            return False
        return bool(re.search(r"改成|改为|换成|加上|增加|删掉|删除|去掉|薪资|地点|职责|技能|要求|福利|经验|学历|React|Vue|Python|Java", message, re.I))

    def _allowed_intents(self):
        """允许的意向列表"""
        return sorted( {*self.tool_registry.keys(),"general"})

    def _format_memory_for_prompt(self, memory_context: str) -> str:
        if not memory_context:
            return ""
        return (
            "历史对话记忆（仅用于理解上下文和指代，不要逐字复述）：n"
            f"{memory_context}nn"
        )

    def _safe_json_loads(self, text:str)->Dict[str,Any]:
        json_text=text.strip()
        if '```' in json_text:
            json_text=re.sub(re.search(r"^```(?:json)?","",json_text,flags=re.IGNORECASE))
            json_text=re.sub(re.search(r"^```$","",json_text,flags=re.IGNORECASE))
        match=re.search(r"{.*}",json_text,re.S)
        if match:
            return json.loads(match.group())
        try:
            value=json.loads(json_text)
            return value if isinstance(value,dict) else {}
        except Exception:
            return {}

    def _decision_to_plan(self, decision: ReActDecision) -> Dict[str, Any]:
        return {
            "mode": decision.mode,
            "intent": decision.intent,
            "action": decision.action,
            "confidence": decision.confidence,
            "reason": decision.thought,
            "observation": decision.observation,
            "reply": decision.reply,
            "source": decision.source,
        }

    def _route_for_intent(self, intent: str, message: str) -> Dict[str, Any]:
        tool = self.tool_registry.get(intent)
        return {
            "intent": intent,
            "route": tool.route if tool else None,
            "query": message,
            "kb_id": None,
        }

    def _planning_step(self, selected_tool, param):
        pass

    def _jd_edit_stream_event(
        self,
        event_type: str,
        message: str,
        steps: List[AgentStep],
        artifacts: List[AgentArtifact],
    ) -> Dict[str, Any]:
        return {
            "type": event_type,
            "response": AgentChatResponse(
                message=message,
                intent="jd_edit",
                route="/recruitment/jd-generator",
                steps=steps,
                artifacts=artifacts,
                suggestions=[],
            ).model_dump(),
        }

    async def _resolve_recent_jd_candidates(
            self,
            user_id: UUID,
            conversation_id: Optional[str],
            keyword: str = "",
    ) -> List[Dict[str, Any]]:
        pending_target = await self._recent_pending_jd_edit_target(user_id, conversation_id)
    async def _recent_pending_jd_edit_target(
        self,
        user_id: UUID,
        conversation_id: Optional[str],
        limit: int = 8,
    ) -> Optional[Dict[str, Any]]:
        conditions = [
            Conversation.user_id == user_id,
            Message.role == MessageRole.ASSISTANT,
        ]
        if conversation_id:
            try:
                conditions.append(Message.conversation_id == UUID(str(conversation_id)))
            except Exception:
                logger.warning("JD 修改上下文收到非法 conversation_id: %s", conversation_id)
                return None

    async def _handle_resource_delete(
            self,
            message:str,
            user_id:UUID,
            selected_tool:Optional[AgentToolSpec],
            conversation_id:Optional[str]=None):
        delete_request=await  self._parse_delete_request(message)
        resource_type = delete_request.get("type")
        keyword = delete_request.get("keyword") or ""
        if (
            resource_type == "resume"
            and not delete_request.get("confirm_context_low_scores")
            and re.search(r"低分|不合格|小于\s*60|低于\s*60|没通过|未通过", message)
        ):
            fllowup_action=await  self._resolve_resume_screening_followup_action(message,user_id,conversation_id)



    async def _parse_delete_request(self, message:str)->Dict[str,Any]:
        prompt=(
            f"""
            你是HR Agent的删除请求解析器.请从用户消息中识别要删除的招聘资源，并严格返回JSON，不要解释。
            resource_type 只是 jd、resume、interview、exam、unknown
            含义：
            - jd:JD、岗位、职位、职位描述、招聘需求、岗位说明书
            - resume:简历、候选人、简历评分/筛选记录。
            - interview:面试方案、面试计划、面试安排
            - exam:试卷、考试、笔试题、测评题
            keyword 填用户用于定位目标的名称/关键词，例如"AI训练师" "张三" "产品经理" "java基础"。不要包含“删除、帮我、岗位、试卷”等动作词或类型词
            context_reference 表示用户是否用“这个/这份/这条/该/刚才/刚生成的/上一个/它”等指代最近生成的产物
            latest 为用户是否明确表达全部/上一个/最近
            delete_all 为用户是否明确表达全部/所有/都删
            confirm_context_low_scores 表达用户是否在上文低分候选人确认后，明确说“确定删除这些低分候选人/确认删除不合格简历”
            如果资源类型不明确，resource_type 返回unknown。
            返回格式：{"resource_type":"jd|resume|interview|exam|unknown","keyword":"...","context_reference":false,"latest":false,"delete_all":false,"confirm_context_low_scores":false}
            用户消息:{message}
            """
        )
        try:
            if self.llm_service is None:
                self.llm_service = LLMService()
            response = await self.llm_service.generate_response(prompt)
            parsed = self._safe_json_loads(response)
            resource_type = str(parsed.get("resource_type") or parsed.get("type") or "").strip().lower()
            if resource_type in {"jd", "resume", "interview", "exam"}:
                keyword = self._clean_optional_value(parsed.get("keyword")) or ""
                return {
                    "type": resource_type,
                    "keyword": keyword,
                    "context_reference": bool(parsed.get("context_reference")),
                    "latest": bool(parsed.get("latest")),
                    "delete_all": bool(parsed.get("delete_all")),
                    "confirm_context_low_scores": bool(parsed.get("confirm_context_low_scores")),
                    "source": "llm",
                }
        except Exception as exc:
            logger.warning("删除请求大模型解析失败，使用规则兜底: %s", exc)
        return self._fallback_parse_delete_request(message)

    def _fallback_parse_delete_request(
            self,
            message:str
    )->Dict[str,Any]:
        lowered=message.lower()
        resource_type=None
        if re.search(r"jd|职位描述|岗位说明书|招聘需求|职位|岗位", lowered):
            resource_type = "jd"
        elif re.search(r"面试方案|面试计划|面试", lowered):
            resource_type = "interview"
        elif re.search(r"试卷|考试|笔试", lowered):
            resource_type = "exam"
        elif re.search(r"简历|候选人", lowered):
            resource_type = "resume"
        keyword = re.sub(r"请|帮我|麻烦|一下|这个|这份|这条|记录|生成的|已生成的", "", message, flags=re.I)
        keyword = re.sub(r"删除|删掉|移除|清理|取消", "", keyword, flags=re.I)
        keyword = re.sub(r"jd|职位描述|岗位说明书|招聘需求|职位|岗位|简历评分|简历记录|简历|候选人|面试方案|面试计划|面试|试卷|考试|笔试", "", keyword, flags=re.I)
        keyword = keyword.strip(" 的：:，,。.?？!！「」『』【】[]()（）")
        return {
            "type": resource_type,
            "keyword": keyword,
            "context_reference": bool(re.search(r"这个|这份|这条|该|刚才|刚刚|上一个|上一条|最近|最新|它|其|刚生成", message)),
            "latest": bool(re.search(r"最近|最新|刚才|上一个|最后", message)),
            "delete_all": bool(re.search(r"全部|所有|都删|全删", message)),
            "confirm_context_low_scores": bool(re.search(r"确认|确定", message) and re.search(r"低分|不合格|没通过|未通过|这些", message)),
            "source": "fallback",
        }

    async def _resolve_resume_screening_followup_action(
            self,
            message:str,
            user_id:UUID,
            conversation_id:Optional[str]):
            if not self._is_followup_resume_screening_loop_request(message):
                return {"action": None}
            groups = await self._recent_resume_screening_groups(conversation_id, user_id, threshold=60)



    def _is_followup_resume_screening_loop_request(self, message)->bool:
        return bool(
            re.search(r"高分|通过|合格|大于\s*60|超过\s*60|60\s*分以上|分数高|低分|不合格|小于\s*60|低于\s*60", message)
            and re.search(r"面试|方案|计划|删除|淘汰|候选人|简历", message)
        )

    async def _recent_resume_screening_groups(self, conversation_id, user_id, threshold):
        pass

















































