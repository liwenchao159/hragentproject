import dataclasses
import importlib.util
import inspect
import json
import logging
from pathlib import Path
from typing import Optional, Any

import pytest
import uvicorn

from app.service.llm_service import LLMService

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class SkillMetadata:
    name: str
    description: str
    intent: str
    route: Optional[str]
    prerequisites: tuple[str, ...]
    phases: tuple[str, ...]
    default_phase: str
    confirmation_action: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class SkillPhaseDefinition:
    """事项名字"""
    phase_name: str
    """脚本路径"""
    script_path: Path
    """方法名字"""
    function_name: str


class ScriptedSkillPhase:
    """skill.md内容读取"""

    def __init__(self, skill_dir: Path, definition: SkillPhaseDefinition):
        self.skill_dir = skill_dir
        self.definition = definition

    @property
    def skill_markdown_path(self):
        return self.skill_dir / "SKILL.md"

    def load_skill_instructions(self):
        try:
            return self.skill_markdown_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            logger.warning("SKILL文件不存在:%s", self.skill_markdown_path)
            return ""

    async def run(self, context: dict[str, Any]) -> dict[str, Any]:
        module_name = f"skill_{self.skill_dir.name}_{self.definition.phase_name}".replace("-", "_")
        spec = importlib.util.spec_from_file_location(module_name, self.definition.script_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载skill脚本:{self.skill_markdown_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = getattr(module, self.definition.function_name)
        if fn is None:
            raise RuntimeError(f"skill脚本缺少函数{self.definition.function_name}:{self.definition.script_path}")

        enriched_context = {
            **context,
            "skill_dir": self.skill_dir,
            "skill_markdown": self.load_skill_instructions(),
            "phase_name": self.definition.phase_name,
        }
        result = fn(enriched_context)
        result = fn(enriched_context)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise RuntimeError(
                f"skill phase 必须返回 dict: {self.definition.script_path}#{self.definition.function_name}")
        return result


@dataclasses.dataclass
class AgentSkillBundle:
    intent: str
    bundle_name: str
    skill_dir: Path
    metadata: SkillMetadata
    phases: dict[str, ScriptedSkillPhase] = dataclasses.field(default_factory=dict)

    def get_phase(self, phase: str) -> ScriptedSkillPhase:
        if phase not in self.phases:
            raise KeyError(f"Skill bundle {self.bundle_name} 未注册 phase: {phase}")
        return self.phases[phase]

    def resolve_phase(self, confirmed_requirements: Optional[dict[str, Any]] = None) -> str:
        action = str((confirmed_requirements or {}).get("action") or "").strip()
        if self.metadata.confirmation_action and action == self.metadata.confirmation_action and "send" in self.phases:
            return "send"
        return self.metadata.default_phase


class AgentSkillDispatcher:
    """根据intent 和phase 分发到skill bundle."""

    def __init__(self, bundles: dict[str, AgentSkillBundle]):
        self.bundles = bundles

    def get_bundle(self, intent: str) -> AgentSkillBundle:
        """获取对应的执行包"""
        if intent not in self.bundles:
            raise KeyError(f"未找到 intent 对应的Skill Bundle：{intent}")
        return self.bundles[intent]

    def dispatch(self, intent: str, phase: str) -> ScriptedSkillPhase:
        return self.get_bundle(intent).get_phase(phase)

    def match_confirmation_action(self, action: str) -> Optional[AgentSkillBundle]:
        for bundle in self.bundles.values():
            if bundle.metadata.confirmation_action == action:
                return bundle
        return None


def parse_phase_script_spec(skill_dir: Path, phase: str) -> Optional[SkillPhaseDefinition]:
    """脚本定义"""
    manifest_path = skill_dir / "skill.json"
    try:
        data=json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return  None
    except json.decoder.JSONDecodeError:
        return None
    phase_scripts=data.get("phase_scripts") or {}
    raw=str(phase_scripts.get(phase) or "")
    if  not raw or ":" not in raw:
        return None
    rel_path,function_name=raw.split(":", maxsplit=1)
    return SkillPhaseDefinition(
        phase_name=phase,
        script_path=skill_dir / rel_path.strip(),
        function_name=function_name.strip(),
    )


def parse_skill_metadata(skill_markdown_path: Path) -> Optional[dict[str, Any]]:
    """获取skillMd的名字和说明"""
    try:
        content=skill_markdown_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    if not content.startswith("---\n"):
        return None
    parts = content.strip().split("\n---\n")
    if len(parts) != 2:
        return None
    frontmatter=parts[0].removeprefix("---\n")
    data:dict[str,str]={}
    for line in frontmatter.splitlines():
        stripped=line.strip()
        if not stripped or ":" not in stripped:
            continue
        key,value=stripped.split(":", maxsplit=1)
        data[key.strip()]=value.strip()
    name=data.get("name","").strip()
    description=data.get("description","").strip()
    if not name or not description:
        return None
    return  {
        "name": name,
        "description": description,
    }


def parse_skill_manifest(skill_dir: Path) -> Optional[SkillMetadata]:
    """获取skill json的说明"""
    manifest_path = skill_dir / "skill.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.decoder.JSONDecodeError as exc:
        logger.warning("skill manifest 解析失败:%s", exc)
        return None
    name = str(data.get("name") or skill_dir.name).strip()
    intent = str(data.get("intent") or "").strip()
    route = str(data.get("route") or "").strip()
    phases = tuple(str(item).strip() for item in data.get("phases") or [] if str(item).strip())
    prerequisites = tuple(str(item).strip() for item in data.get("prerequisites") or [] if str(item).strip())
    default_phase = str(data.get("default_phase") or "")
    confirmation_action = str(data.get("confirmation_action") or "").strip()
    phase_scripts_raw = data.get("phase_scripts") or []
    if not name or not intent or not phases or not default_phase or not isinstance(phase_scripts_raw, dict):
        return None
    description=""
    skill_md=parse_skill_metadata(skill_dir/"SKILL.md")

    if skill_md:
        description=skill_md["description"]
    return SkillMetadata(
        name=name,
        description=description,
        intent=intent,
        route=route,
        prerequisites=prerequisites,
        phases=phases,
        default_phase=default_phase,
        confirmation_action=confirmation_action
    )

def build_skill_bundle_from_directory(skill_dir:Path) -> Optional[AgentSkillBundle]:
    metadata = parse_skill_manifest(skill_dir)
    if not metadata:
        return None
    phases: dict[str:ScriptedSkillPhase] = {}
    for phase in metadata.phases:
        phase_spec=parse_phase_script_spec(skill_dir, phase)
        if not phase_spec:
            logger.warning("Skill %s 缺少 phase 脚本定义 %s",metadata.name, phase)
            continue
        phases[phase] = ScriptedSkillPhase(skill_dir,phase_spec)
    if not phases:
        return None

    return AgentSkillBundle(
        intent=metadata.intent,
        bundle_name=metadata.name,
        skill_dir=skill_dir,
        metadata=metadata,
        phases=phases,
    )


def build_default_skill_dispatcher(
        skill_root: Optional[Path] = None
) -> AgentSkillDispatcher | None:
    root = Path(skill_root) if skill_root else Path(__file__).resolve().parents[2] / "skills"
    bundles: dict[str, AgentSkillBundle] = {}
    if not root.exists():
        return AgentSkillDispatcher(bundles)
    for skill_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        bundle = build_skill_bundle_from_directory(skill_dir)
        if not bundle:
            continue
        bundles[bundle.intent] = bundle
    return AgentSkillDispatcher(bundles)



import asyncio

# 补全你的业务导入
# from your_module import build_default_skill_dispatcher, LLMService
if __name__ == '__main__':
    @pytest.mark.asyncio
    async def test():
        bundles = build_default_skill_dispatcher()
        email_bundle = bundles.get_bundle("email_notification")
        draft_phase = email_bundle.get_phase("draft")
        # 异步执行阶段逻辑
        output = await draft_phase.run({
            "llm_service": LLMService(),
            "message": "写封招聘邮件",
        })
        print("邮件生成结果：", output)
