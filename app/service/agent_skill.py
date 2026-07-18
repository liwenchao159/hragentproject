import dataclasses
import importlib.util
import inspect
import json
import logging
from pathlib import Path
from typing import Optional, Any

from aiohttp.web_routedef import route

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
    async def run(self,context:dict[str,Any])->dict[str,Any]:
        module_name = f"skill_{self.skill_dir.name}_{self.definition.phase_name}".replace("-", "_")
        spec=importlib.util.spec_from_file_location(module_name,self.definition.script_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载skill脚本:{self.skill_markdown_path}")
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn=getattr(module,self.definition.function_name)
        if fn is None:
            raise  RuntimeError(f"skill脚本缺少函数{self.definition.function_name}:{self.definition.script_path}")

        enriched_context={
            **context,
            "skill_dir":self.skill_dir,
            "skill_markdown":self.load_skill_instructions(),
            "phase_name":self.definition.phase_name,
        }
        result=fn(enriched_context)
        result = fn(enriched_context)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, dict):
            raise RuntimeError(f"skill phase 必须返回 dict: {self.definition.script_path}#{self.definition.function_name}")
        return result

@dataclasses.dataclass
class AgentSkillBundle:
    intent: str
    bundle_name: str
    skill_dir: Path
    metadata: SkillMetadata
    phases: dict[str, ScriptedSkillPhase] = dataclasses.field(default_factory=dict)
    def get_phase(self,phase:str)->ScriptedSkillPhase:
        if phase not in self.phases:
            raise KeyError(f"Skill bundle {self.bundle_name} 未注册 phase: {phase}")
        return self.phases[phase]

    def resolve_phase(self,confirmed_requirements: Optional[dict[str, Any]] = None)->str:
        action = str((confirmed_requirements or {}).get("action") or "").strip()
        if self.metadata.confirmation_action and action == self.metadata.confirmation_action and "send" in self.phases:
            return "send"
        return self.metadata.default_phase


class AgentSkillDispatcher:
    """根据intent 和phase 分发到skill bundle."""
    def __init__(self, bundles:dict[str,AgentSkillBundle]):
        self.bundles = bundles
    def get_bundle(self,intent:str)->AgentSkillBundle:
        """获取对应的执行包"""
        if intent not in self.bundles:
            raise KeyError(f"未找到 intent 对应的Skill Bundle：{intent}")
        return self.bundles[intent]
    def dispatch(self,intent:str,phase:str)->ScriptedSkillPhase:
        return self.get_bundle(intent).get_phase(phase)
    def match_confirmation_action(self, action: str) -> Optional[AgentSkillBundle]:
        for bundle in self.bundles.values():
            if bundle.metadata.confirmation_action == action:
                return bundle
        return None


def parse_phase_script_spec(skill_dir: Path, phase: str) -> Optional[SkillPhaseDefinition]:
    pass


def parse_skill_manifest(skill_dir:Path)->Optional[SkillMetadata]:
    manifest_path=skill_dir/"skill.json"
    try:
        data=json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.decoder.JSONDecodeError as exc:
        logger.warning("skill manifest 解析失败:%s",exc)
        return None
    name = str(data.get("name") or skill_dir.name).strip()
    intent=str(data.get("intent") or "").strip()
    route=str(data.get("route") or "").strip()
    phases=tuple(str(item).strip() for item in data.get("phases") or [] if str(item).strip())
    prerequisites=tuple(str(item).strip() for item in data.get("prerequisites") or [] if str(item).strip())

    description=str(data.get("description") or "").strip()




def build_skill_bundle_from_directory(skill_dir)-> None:
    metadata=parse_skill_manifest(skill_dir)
    if not metadata:
        return None
    phases:dict[str:ScriptedSkillPhase]={}
    for phases in metadata.phases:





def build_default_skill_dispatcher(
        skill_root: Optional[Path] = None
) -> AgentSkillDispatcher | None:
    root = Path(skill_root) if skill_root else Path(__file__).resolve().parent[2] / "skills"
    bundles: dict[str, AgentSkillBundle] = {}
    if not root.exists():
        return AgentSkillDispatcher(bundles)
    for skill_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        bundle = build_skill_bundle_from_directory(skill_dir)
        if not  bundle:
            continue
        bundles[bundle.intent] = bundle


