"""Trusted host capabilities; policy prose and MCP discovery cannot expand them."""
from __future__ import annotations

import httpx


DOMAIN_TOOLS = {
    "weather": {"maps_weather"},
    "navigation": {"maps_search", "maps_route"},
    "music": {"search_music"},
    "local_life": {"maps_local_search", "maps_place_detail", "maps_route"},
    "planning": {"save_itinerary", "create_reminder", "list_itineraries", "list_reminders", "cancel_reminder"},
    "chat": set(), "wellbeing": set(),
}


def normalize_tool_result(result: object) -> dict:
    if not isinstance(result, dict):
        raise TypeError("工具返回必须是结构化对象")
    for flag in ("ok", "success", "requires_clarification"):
        if flag in result and type(result[flag]) is not bool:
            raise TypeError(f"工具回执 {flag} 必须为布尔值")
    if "ok" in result and "success" in result and result["ok"] != result["success"]:
        raise TypeError("工具回执成功标记互相冲突")
    if result.get("success") is False:
        return {**result, "ok": False, "error": result.get("error") or "工具报告业务失败",
                "error_code": result.get("error_code") or "UPSTREAM_BUSINESS"}
    return result


def require_tool(domain: str, action: str, name: str) -> None:
    allowed = DOMAIN_TOOLS.get(domain, set())
    if name not in allowed or (domain == "planning" and name != action):
        raise PermissionError(f"当前领域或动作未授权工具：{name}")


def retryable_error(exc: Exception, result: dict | None = None) -> tuple[str, bool]:
    result = result or {}
    code = str(result.get("error_code") or "")
    forbidden = {"INVALID_ARGUMENT", "UNKNOWN_TOOL", "NOT_CONFIGURED", "NOT_FOUND", "DATE_UNAVAILABLE",
                 "IDEMPOTENCY_CONFLICT", "UPSTREAM_SCHEMA", "PERMISSION_DENIED", "BUDGET_EXCEEDED"}
    if code in forbidden or code in {"AMAP_10001", "AMAP_10002", "AMAP_10005", "AMAP_10009", "AMAP_10012"}:
        return code, False
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "UPSTREAM_TIMEOUT", True
    if isinstance(exc, (httpx.TransportError, ConnectionError)):
        return "UPSTREAM_NETWORK", True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return f"HTTP_{status}", status == 429 or status >= 500
    # Explicit adapter classifications, not arbitrary programming exceptions, may retry.
    return code or type(exc).__name__, bool(code and result.get("retryable") is True)
