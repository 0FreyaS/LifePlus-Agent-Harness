from typing import Annotated, Any, Literal
import unicodedata

from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field, field_validator
from typing_extensions import NotRequired, TypedDict


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    mode: Literal["demo", "live"] = "live"
    request_id: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("message", mode="before")
    @classmethod
    def normalize_message(cls, value):
        if not isinstance(value, str):
            return value
        value = unicodedata.normalize("NFC", value).strip()
        if any(unicodedata.category(char) == "Cc" and char not in "\n\r\t" for char in value):
            raise ValueError("消息包含不支持的控制字符")
        return value


class ResumeRequest(BaseModel):
    interrupt_id: str = Field(min_length=1, max_length=255)
    action: Literal["approve", "reject", "answer"]
    value: dict[str, Any] = Field(default_factory=dict)

    @field_validator("value")
    @classmethod
    def validate_text(cls, value):
        if "text" in value:
            if not isinstance(value["text"], str):
                raise ValueError("回复文本必须是字符串")
            text = ChatRequest.normalize_message(value["text"])
            if len(text) > 4000:
                raise ValueError("回复文本不能超过4000字符")
            value = {**value, "text": text}
        return value


class LocalLifeRequest(BaseModel):
    session_id: str
    action: Literal["local_search", "local_detail", "local_route"]
    parameters: dict[str, Any]
    request_id: str | None = Field(default=None, min_length=1, max_length=255)
    mode: Literal["demo", "live"] = "live"


class RefreshRequest(BaseModel):
    interrupt_id: str = Field(min_length=1, max_length=255)


class RouteReplanRequest(BaseModel):
    model_config = {"extra": "forbid"}
    job_id: str = Field(min_length=1, max_length=255)
    travel_mode: Literal["driving", "walking", "bicycling"]
    request_id: str = Field(min_length=1, max_length=255)


class AgentState(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    user_id: str
    session_id: str
    task_id: str
    request_id: str
    mode: str
    original_query: str
    resolved_query: str
    intent: str
    domain: str
    action: str
    slots: dict[str, Any]
    skill: str
    tool_name: str
    tool_args: dict[str, Any]
    tool_result: dict[str, Any]
    operation_result: dict[str, Any]
    operation_key: str
    memory: dict[str, str]
    memory_brief: str
    conversation_references: list[dict[str, Any]]
    model_history: list[dict[str, str]]
    skill_names: list[str]
    handoff_summary: dict[str, Any]
    rag_context: list[dict[str, Any]]
    answer: str
    status: str
    interrupt: NotRequired[dict[str, Any]]
    error: NotRequired[str]
    prepared_decision: dict[str, Any]
    execution_verification: dict[str, Any]
    read_generation: str
    approval_expires_at: str
