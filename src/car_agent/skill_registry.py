from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillBundle:
    names: list[str]
    instructions: str
    risk_level: str = "normal"
    handoff_summary: dict[str, Any] | None = None


class LifeSkillRegistry:
    """Loads auditable business policy text independently from graph code."""

    HIGH_RISK_MARKERS = (
        "胸痛", "呼吸困难", "昏迷", "大量出血", "过敏性休克", "自杀", "轻生",
        "人身安全", "威胁", "投诉升级", "监管投诉",
    )

    def __init__(self, root: str):
        self.root = Path(root)
        self._cache: dict[str, tuple[int, str]] = {}

    def _load(self, name: str) -> str:
        path = self.root / name / "SKILL.md"
        if not path.exists():
            return ""
        modified = path.stat().st_mtime_ns
        cached = self._cache.get(name)
        if cached and cached[0] == modified:
            return cached[1]
        content = path.read_text(encoding="utf-8").strip()
        self._cache[name] = (modified, content)
        return content

    def resolve(self, domain: str, query: str, state: dict[str, Any] | None = None) -> SkillBundle:
        names = [domain] if (self.root / domain / "SKILL.md").exists() else ["chat"]
        risk_level = "normal"
        handoff = None
        if any(marker in query for marker in self.HIGH_RISK_MARKERS):
            names.append("safety")
            risk_level = "high"
            state = state or {}
            handoff = {
                "request": query[:500],
                "risk_signals": [marker for marker in self.HIGH_RISK_MARKERS if marker in query],
                "known_context": state.get("memory_brief", ""),
                "completed_actions": [],
                "pending_action": "转交人工服务并保留当前 Trace",
            }
        content = "\n\n".join(self._load(name) for name in names if self._load(name))
        return SkillBundle(names=names, instructions=content, risk_level=risk_level,
                           handoff_summary=handoff)

    def describe(self) -> list[dict[str, str]]:
        return [
            {
                "name": path.parent.name,
                "path": str(path),
                "content_hash": hashlib.sha256(
                    self._load(path.parent.name).encode("utf-8")
                ).hexdigest()[:16],
            }
            for path in sorted(self.root.glob("*/SKILL.md"))
        ]

    @staticmethod
    def handoff_text(summary: dict[str, Any] | None) -> str:
        return json.dumps(summary or {}, ensure_ascii=False, sort_keys=True)
