"""TypeSafe System One choices, independent of chat-completion clients."""
from __future__ import annotations

import asyncio
import math
from time import monotonic
from typing import Any, Literal
from uuid import uuid4

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..runtime_limits import consume, current_scope, record


class JevUnavailable(RuntimeError):
    pass


class ChoiceAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: Literal["choice"]
    choice: str
    probabilities: dict[str, float] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def valid_distribution(self):
        values = self.probabilities
        if self.choice not in values or any(not math.isfinite(p) or not 0 <= p <= 1 for p in values.values()):
            raise ValueError("Invalid choice probabilities")
        if not math.isclose(sum(values.values()), 1.0, abs_tol=0.02):
            raise ValueError("Choice probabilities must sum to one")
        if values[self.choice] < max(values.values()):
            raise ValueError("Choice does not match the probability maximum")
        return self


class ChoiceResponse(BaseModel):
    model: str = Field(min_length=1)
    answers: dict[str, ChoiceAnswer]
    usage: dict[str, Any] = Field(default_factory=dict)


class JevClient:
    def __init__(self, api_key: str, *, base_url: str = "https://api.typesafe.ai/v1",
                 model: str = "jev-1.14.0", timeout: float = 1.5,
                 transport: httpx.AsyncBaseTransport | None = None):
        if not api_key.strip():
            raise ValueError("JEV_API_KEY is required")
        self.model = model
        self.timeout = timeout
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/", timeout=timeout,
            headers={"Authorization": "Bearer " + api_key}, transport=transport,
        )

    async def close(self):
        await self.client.aclose()

    async def choose(self, state: dict, criteria: dict[str, str], instructions: str, *,
                     question: str = "domain", stage: str = "domain_route_precheck") -> ChoiceResponse:
        return await self.choose_many(state, {question: {"type": "choice", "criteria": criteria,
            "instructions": instructions}}, stage=stage)

    async def choose_many(self, state: dict, questions: dict[str, dict], *, stage: str) -> ChoiceResponse:
        if not questions or any(q.get("type") != "choice" or not q.get("criteria") for q in questions.values()):
            raise ValueError("Nonempty choice questions required")
        scope = current_scope()
        if scope:
            await consume(scope.db, scope.task_id, "model_calls", scope.limits.model_calls)
        timeout = min(self.timeout, scope.remaining()) if scope else self.timeout
        span_id, started = str(uuid4()), monotonic()
        usage = {}
        await record("model.attempt.started", stage=stage, attempt=1, span_id=span_id,
                     provider="typesafe", model_name=self.model)
        try:
            async with asyncio.timeout(timeout):
                response = await self.client.post("systemone", json={
                    "model": self.model, "state": state,
                    "questions": questions,
                }, timeout=timeout)
                response.raise_for_status()
                raw = response.json()
                raw_usage = raw.get("usage") if isinstance(raw, dict) else None
                if isinstance(raw_usage, dict):
                    usage = {key: value for key in ("input_tokens", "output_tokens")
                             if type(value := raw_usage.get(key)) is int and value >= 0}
                result = ChoiceResponse.model_validate(raw)
                if set(result.answers) != set(questions) or any(
                    set(result.answers[key].probabilities) != set(value["criteria"]) for key, value in questions.items()
                ):
                    raise ValueError("Jev returned a different choice contract")
        except asyncio.CancelledError:
            await record("model.attempt.failed", stage=stage, attempt=1, span_id=span_id,
                         provider="typesafe", elapsed_ms=round((monotonic() - started) * 1000),
                         error_type="CancelledError", usage={}, usage_known=False)
            raise
        except (httpx.HTTPError, TimeoutError, ValueError) as exc:
            await record("model.attempt.failed", stage=stage, attempt=1, span_id=span_id,
                         provider="typesafe", elapsed_ms=round((monotonic() - started) * 1000),
                         error_type=type(exc).__name__, usage=usage, usage_known=len(usage) == 2,
                         http_status=exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None)
            # Do not put provider bodies or headers in user-facing errors or traces.
            raise JevUnavailable("Jev decision unavailable: " + type(exc).__name__) from None
        await record("model.attempt.completed", stage=stage, attempt=1, span_id=span_id,
                     provider="typesafe", model_name=result.model,
                     elapsed_ms=round((monotonic() - started) * 1000),
                     usage=usage, usage_known=len(usage) == 2)
        return result
