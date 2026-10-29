"""Role-specific model clients; credentials and transport stay host configured."""
from __future__ import annotations

from typing import Any


def select_model(context: Any, role: str):
    return (getattr(context, "model_roles", None) or {}).get(role, context.llm)


def build_models(settings: Any) -> tuple[Any, dict[str, Any]]:
    if not (settings.model_base_url and settings.model_api_key and settings.model_name):
        return None, {}
    from langchain_openai import ChatOpenAI

    def create(name: str, tokens: int):
        return ChatOpenAI(base_url=settings.model_base_url, api_key=settings.model_api_key,
                          model=name, temperature=0, timeout=25, max_retries=0,
                          max_tokens=tokens, stream_usage=settings.model_stream_usage)

    default = create(settings.model_name, settings.model_max_output_tokens)
    roles = {}
    for role in ("planner", "intent", "response"):
        name = getattr(settings, role + "_model_name")
        tokens = getattr(settings, role + "_max_output_tokens")
        # Baseline uses the original client unless explicitly overridden.
        enabled = name or (role == "planner" and settings.planner_variant == "compact")
        if enabled or (role != "planner" and tokens is not None):
            roles[role] = create(name or settings.model_name, tokens or settings.model_max_output_tokens)
    return default, roles
