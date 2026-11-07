from typing import Any
from time import monotonic
from uuid import uuid4

from langgraph.config import get_stream_writer
from .runtime_limits import consume, current_scope, record


def text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") in {None, "text", "output_text"})
    return ""


async def stream_reply(llm: Any, messages: list, stream_id: str) -> str:
    """Forward real model deltas; the final graph response remains the authoritative answer."""
    writer = get_stream_writer()
    scope = current_scope()
    if scope:
        await consume(scope.db, scope.task_id, "model_calls", scope.limits.model_calls)
    started, first_delta, span_id = monotonic(), None, str(uuid4())
    await record("model.attempt.started", stage="response", span_id=span_id,
                 model_name=getattr(llm, "model_name", None), framework_traced=True)
    writer({"type": "answer.started", "stream_id": stream_id})
    answer = ""
    usage = {}
    try:
        config = None
        if scope:
            sink = getattr(scope.db, "event_sink", None)
            handler = sink.callback_handler(scope.task_id) if sink else None
            if handler:
                config = {"callbacks": [handler], "run_name": "generate-response"}
        if callable(getattr(llm, "astream", None)):
            stream = llm.astream(messages, config=config) if config else llm.astream(messages)
            async for chunk in stream:
                if getattr(chunk, "usage_metadata", None):
                    usage = chunk.usage_metadata
                delta = text_content(getattr(chunk, "content", chunk))
                if delta:
                    if first_delta is None:
                        first_delta = round((monotonic() - started) * 1000)
                    answer += delta
                    writer({"type": "answer.delta", "stream_id": stream_id, "delta": delta})
        else:
            response = await llm.ainvoke(messages, config=config) if config else await llm.ainvoke(messages)
            usage = getattr(response, "usage_metadata", None) or {}
            answer = text_content(response.content)
        if not answer.strip():
            raise RuntimeError("模型返回了空回复")
        await record("model.attempt.completed", stage="response", span_id=span_id,
                     elapsed_ms=round((monotonic() - started) * 1000), first_delta_ms=first_delta,
                     usage=usage, framework_traced=True)
    except BaseException as exc:
        await record("model.attempt.failed", stage="response", span_id=span_id,
                     elapsed_ms=round((monotonic() - started) * 1000), first_delta_ms=first_delta,
                     error_type=type(exc).__name__, usage=usage, usage_known=bool(usage), framework_traced=True)
        raise
    return answer.strip()
