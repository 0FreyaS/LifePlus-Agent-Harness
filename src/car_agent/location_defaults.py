"""Configured location fallback, never a GPS fix or a selected destination."""
from .config import get_settings


RELATIVE_LOCATIONS = {"这里", "这儿", "我这里", "当前位置", "我的位置", "当前所在地", "我所在的位置", "附近"}


def with_default_location(domain: str, action: str, values: dict) -> dict:
    slots = dict(values)
    settings = get_settings()
    location = settings.default_current_location.strip()
    if not location:
        return slots

    def unspecified(field):
        return not str(slots.get(field) or "").strip() or str(slots[field]).strip() in RELATIVE_LOCATIONS

    if domain == "navigation" and slots.get("route_requested") and unspecified("origin"):
        slots["origin"] = location
    if domain == "local_life" and action in {"local_search", "local_route"} and unspecified("anchor"):
        city = str(slots.get("city") or "").strip()
        if not city or city.removesuffix("市") == settings.default_current_city.removesuffix("市"):
            slots["anchor"] = location
            slots["city"] = settings.default_current_city
    return slots
