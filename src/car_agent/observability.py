"""Optional Langfuse export for the harness event stream.

The local event ledger remains authoritative. Langfuse is a best-effort
observability sink and must never change task execution semantics.
"""
from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from typing import Any


LOGGER = logging.getLogger(__name__)
TERMINAL_EVENTS = {
    "task.completed",
    "task.partial",
    "task.failed",
    "task.cancelled",
    "task.interrupted",
}
SECRET_FIELDS = re.compile(
    r"(?:api[_-]?key|authorization|cookie|password|secret|token|credential)", re.IGNORECASE
)
SECRET_VALUES = re.compile(
    r"(?i)(?:sk|pk)-lf-[a-z0-9-]{12,}|(?:xai|sk)-[a-z0-9_-]{16,}|bearer\s+[a-z0-9._~+/-]{12,}"
)
EMAIL = re.compile(r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])")
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
CN_ID = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")


def langfuse_mask(*, data: Any, capture_content: bool) -> Any:
    return sanitize(data, capture_content=capture_content)


def useful_span(span: Any) -> bool:
    """Drop framework plumbing while retaining business and model observations."""
    name = str(getattr(span, "name", ""))
    return name != "<unknown>" and not name.startswith(("Runnable", "PydanticOutputParser"))


def _safe_text(value: str, limit: int = 8000) -> str:
    value = SECRET_VALUES.sub("<redacted-secret>", value)
    value = EMAIL.sub("<redacted-email>", value)
    value = PHONE.sub("<redacted-phone>", value)
    value = CN_ID.sub("<redacted-id>", value)
    return value if len(value) <= limit else value[:limit] + "...<truncated>"


def sanitize(value: Any, *, capture_content: bool, field: str = "", depth: int = 0) -> Any:
    """Bound and redact event payloads before they leave the application."""
    if depth > 8:
        return "<max-depth>"
    if SECRET_FIELDS.search(field):
        return "<redacted>"
    if isinstance(value, str):
        if not capture_content and field.lower() in {
            "query", "answer", "content", "message", "messages", "input", "output", "delta",
        }:
            return {"redacted": True, "characters": len(value)}
        return _safe_text(value)
    if isinstance(value, dict):
        return {
            str(key)[:128]: sanitize(item, capture_content=capture_content, field=str(key), depth=depth + 1)
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item, capture_content=capture_content, field=field, depth=depth + 1)
                for item in list(value)[:100]]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _safe_text(str(value), 2000)


def _opaque_user_id(value: str) -> str:
    return hashlib.sha256(("lifeplus:" + value).encode()).hexdigest()[:24]


def _usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    aliases = {
        "input_tokens": ("input_tokens", "prompt_tokens"),
        "output_tokens": ("output_tokens", "completion_tokens"),
        "total_tokens": ("total_tokens",),
    }
    result = {}
    for target, names in aliases.items():
        raw = next((value[name] for name in names if isinstance(value.get(name), (int, float))), None)
        if raw is not None:
            result[target] = int(raw)
    return result or None


@dataclass
class LangfuseTraceSink:
    client: Any
    public_key: str
    capture_content: bool = False
    hash_user_ids: bool = True
    roots: dict[str, Any] = field(default_factory=dict)
    observations: dict[tuple[str, str, str], Any] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    last_error: str | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> "LangfuseTraceSink | None":
        public = getattr(settings, "langfuse_public_key", None)
        secret = getattr(settings, "langfuse_secret_key", None)
        if not getattr(settings, "langfuse_enabled", True) or not public or not secret:
            return None
        from langfuse import Langfuse

        public_key = public.get_secret_value()

        return cls(
            Langfuse(
                public_key=public_key,
                secret_key=secret.get_secret_value(),
                base_url=settings.langfuse_base_url,
                timeout=int(settings.langfuse_timeout_seconds),
                environment=settings.langfuse_environment,
                release=settings.langfuse_release,
                flush_interval=settings.langfuse_flush_interval_seconds,
                mask=lambda *, data, **_: langfuse_mask(
                    data=data, capture_content=settings.langfuse_capture_content
                ),
                should_export_span=useful_span,
            ),
            public_key=public_key,
            capture_content=settings.langfuse_capture_content,
            hash_user_ids=settings.langfuse_hash_user_ids,
        )

    @property
    def status(self) -> dict[str, Any]:
        return {"enabled": True, "last_error": self.last_error}

    def _trace_id(self, task_id: str) -> str:
        return self.client.create_trace_id(seed="lifeplus-task:" + task_id)

    def _root(self, task: dict[str, Any]) -> Any:
        task_id = task["task_id"]
        root = self.roots.get(task_id)
        if root is not None:
            return root
        trace_id = self._trace_id(task_id)
        query = sanitize(task.get("query", ""), capture_content=self.capture_content, field="query")
        user_id = task.get("user_id") or ""
        from langfuse import propagate_attributes

        with propagate_attributes(
            trace_name="process-lifeplus-request",
            user_id=_opaque_user_id(user_id) if self.hash_user_ids and user_id else user_id or None,
            session_id=task.get("session_id"),
            tags=["lifeplus", task.get("mode") or "unknown"],
            metadata={"task_id": task_id, "request_id": task.get("request_id")},
        ):
            root = self.client.start_observation(
                trace_context={"trace_id": trace_id},
                name="process-lifeplus-task",
                as_type="agent",
                input=query,
                metadata=sanitize({
                    "task_id": task_id,
                    "request_id": task.get("request_id"),
                    "parent_task_id": task.get("parent_task_id"),
                    "mode": task.get("mode"),
                    "domain": task.get("domain"),
                }, capture_content=self.capture_content),
            )
        self.roots[task_id] = root
        return root

    def callback_handler(self, task_id: str):
        """Connect LangChain/LangGraph callbacks to the task's agent root."""
        root = self.roots.get(task_id)
        if root is None:
            return None
        from langfuse.langchain import CallbackHandler

        return CallbackHandler(
            public_key=self.public_key,
            trace_context={"trace_id": root.trace_id, "parent_span_id": root.id},
        )

    @staticmethod
    def _event_type(event_type: str) -> str:
        if event_type.startswith(("approval.", "guardrail.", "tool.policy.")):
            return "guardrail"
        if event_type.startswith("tool."):
            return "tool"
        if event_type.startswith("model."):
            return "generation"
        if event_type.startswith("rag."):
            return "retriever"
        if event_type.startswith(("plan.", "node.", "subtask.")):
            return "chain"
        return "span"

    @staticmethod
    def _correlation(event_type: str, data: dict[str, Any]) -> tuple[str, str] | None:
        if event_type.startswith("model.attempt.") and data.get("span_id"):
            return "model", str(data["span_id"])
        if event_type.startswith("tool.") and data.get("job_id"):
            return "tool", str(data["job_id"])
        if event_type.startswith("node.") and data.get("node"):
            return "node", str(data["node"])
        return None

    def publish(self, task: dict[str, Any], event_type: str, data: dict[str, Any]) -> None:
        """Queue a Langfuse observation; exporter failures never escape this method."""
        try:
            with self.lock:
                self._publish(task, event_type, data)
                self.last_error = None
        except Exception as exc:  # observability must not fail business work
            self.last_error = type(exc).__name__
            LOGGER.warning("Langfuse export skipped: %s", type(exc).__name__)

    def _publish(self, task: dict[str, Any], event_type: str, data: dict[str, Any]) -> None:
        task_id = task["task_id"]
        root = self._root(task)
        # LangChain's callback already creates rich generations and graph spans.
        # Re-exporting lifecycle events would duplicate cost and latency.
        if data.get("framework_traced") and event_type.startswith("model.attempt."):
            return
        if event_type in {
            "answer.started", "answer.delta", "answer.completed", "task.started", "task.queued",
            "runtime.started", "runtime.finished", "plan.started",
        }:
            return
        if ".generation." in event_type and event_type.endswith(("started", "completed")):
            return
        safe = sanitize(data, capture_content=self.capture_content)
        if event_type in TERMINAL_EVENTS:
            answer = sanitize(data.get("answer", ""), capture_content=self.capture_content, field="answer")
            root.update(
                output={"status": event_type.removeprefix("task."), "answer": answer},
                level="ERROR" if event_type == "task.failed" else "DEFAULT",
                status_message=data.get("error"),
            ).end()
            self.roots.pop(task_id, None)
            for pending in [item for item in self.observations if item[0] == task_id]:
                self.observations.pop(pending).update(
                    level="WARNING", status_message="task ended before correlated event completed"
                ).end()
            return
        correlation = self._correlation(event_type, data)
        phase = event_type.rsplit(".", 1)[-1]
        key = (task_id, *correlation) if correlation else None

        if correlation and phase in {"started", "queued"} and key not in self.observations:
            observation = self.client.start_observation(
                trace_context={"trace_id": root.trace_id, "parent_span_id": root.id},
                name=(f"llm.{data.get('stage', 'unknown')}" if correlation[0] == "model"
                      else f"tool.{data.get('tool_name', 'unknown')}" if correlation[0] == "tool"
                      else str(data.get("node", event_type))),
                as_type=self._event_type(event_type),
                input=safe,
                metadata={"lifeplus_event": event_type, "correlation_id": correlation[1]},
                model=data.get("model_name") if correlation[0] == "model" else None,
            )
            self.observations[key] = observation
        elif correlation and key in self.observations and phase in {
            "completed", "failed", "cancelled", "dead_letter", "clarification_required", "budget_exhausted",
        }:
            observation = self.observations.pop(key)
            failed = phase in {"failed", "dead_letter", "budget_exhausted"}
            observation.update(
                output=safe,
                metadata={"lifeplus_event": event_type, **safe},
                level="ERROR" if failed else "DEFAULT",
                status_message=data.get("error_type") or data.get("error"),
                usage_details=_usage(data.get("usage")),
            ).end()
        elif correlation and key in self.observations:
            self.observations[key].update(metadata={"lifeplus_event": event_type, **safe})
        else:
            failed = event_type.endswith((".failed", ".dead_letter", ".budget_exhausted"))
            self.client.start_observation(
                trace_context={"trace_id": root.trace_id, "parent_span_id": root.id},
                name=event_type,
                as_type=self._event_type(event_type),
                output=safe,
                metadata={"lifeplus_event": event_type},
                level="ERROR" if failed else "DEFAULT",
                status_message=data.get("error_type") or data.get("error"),
            ).end()

    def flush(self) -> None:
        try:
            self.client.flush()
        except Exception as exc:
            self.last_error = type(exc).__name__
            LOGGER.warning("Langfuse flush failed: %s", type(exc).__name__)

    def shutdown(self) -> None:
        with self.lock:
            for observation in list(self.observations.values()):
                observation.update(level="WARNING", status_message="application shutdown").end()
            for root in list(self.roots.values()):
                root.update(level="WARNING", status_message="application shutdown").end()
            self.observations.clear()
            self.roots.clear()
            try:
                self.client.shutdown()
            except Exception as exc:
                self.last_error = type(exc).__name__
                LOGGER.warning("Langfuse shutdown failed: %s", type(exc).__name__)
