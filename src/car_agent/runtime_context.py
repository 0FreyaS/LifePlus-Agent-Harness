from dataclasses import dataclass, field
from typing import Any, Callable

from .adapters.demo import DemoServices
from .adapters.mcp_client import LocalMCPClient
from .memory import MemoryRepository
from .persistence import Database
from .rag import KnowledgeRetriever
from .skill_registry import LifeSkillRegistry
from .tool_queue import ToolQueue
from .freshness import FreshnessPolicy
from .runtime_limits import RuntimeLimits
from .model_roles import select_model


@dataclass
class RunContext:
    user_id: str
    db: Database
    memory: MemoryRepository
    demo: DemoServices
    mcp: LocalMCPClient
    tool_queue: ToolQueue
    skill_registry: LifeSkillRegistry
    retriever: KnowledgeRetriever
    llm: Any = None
    skills_dir: str = "./skills"
    freshness: FreshnessPolicy = field(default_factory=FreshnessPolicy)
    limits: RuntimeLimits = field(default_factory=RuntimeLimits)
    model_roles: dict[str, Any] = field(default_factory=dict)
    planner_variant: str = "baseline"

    def model_for(self, role: str):
        return select_model(self, role)

