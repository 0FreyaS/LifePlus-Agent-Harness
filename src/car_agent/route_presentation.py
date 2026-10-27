"""Authenticated route receipts and server-side AMap image rendering."""
from __future__ import annotations

import asyncio
import json
import math
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from time import monotonic
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from .freshness import expired
from .route_geometry import coordinate_text, point, sample_points
from .travel_modes import mode_choices


def route_record(row: dict, route_seconds: float = 120, option: int | None = None) -> dict | None:
    try:
        result = json.loads(row.get("result_json") or "{}")
        if not isinstance(result, dict):
            return None
        if result.get("ok") is not True or result.get("error") or result.get("simulated") or result.get("source") != "高德地图":
            return None
        choices = []
        alternatives = result.get("alternatives")
        if isinstance(alternatives, list) and alternatives:
            for index, candidate in enumerate(alternatives[:5]):
                if not isinstance(candidate, dict) or not all(key in candidate for key in ("distance_m", "duration_s")):
                    continue
                child_result = {key: value for key, value in result.items() if key != "alternatives"}
                child_result.update(geometry={}, steps=[], step_count=0, steps_truncated=False, tolls="0")
                child_result.update({key: candidate[key] for key in ("distance_m", "duration_s", "tolls", "steps", "step_count", "steps_truncated", "geometry") if key in candidate})
                child = route_record({**row, "result_json": json.dumps(child_result)}, route_seconds)
                if child:
                    identifier = candidate.get("option_id", index)
                    if not isinstance(identifier, int) or isinstance(identifier, bool) or not 0 <= identifier < 5 or any(item["option_id"] == identifier for item in choices):
                        continue
                    child.update(option_id=identifier, strategy=str(candidate.get("strategy") or "")[:100])
                    choices.append(child)
            if not choices:
                return None
            selected = next((child for child in choices if child["option_id"] == option), None) if option is not None else choices[0]
            if selected is None:
                return None
            selected["options"] = [{"option_id": child["option_id"], "label": f"方案 {i + 1}",
                **{key: child[key] for key in ("distance_m", "duration_s", "tolls", "strategy", "geometry_available")}} for i, child in enumerate(choices)]
            selected["alternatives_truncated"] = bool(result.get("alternatives_truncated"))
            return selected
        if option not in (None, 0):
            return None
        origin, destination = coordinate_text(result["origin"]), coordinate_text(result["destination"])
        distance, duration = float(result["distance_m"]), float(result["duration_s"])
        if not all(math.isfinite(number) and number > 0 for number in (distance, duration)):
            return None
        mode = result.get("travel_mode", "driving")
        if mode not in {"driving", "walking", "bicycling"}:
            return None
        geometry = result.get("geometry") if isinstance(result.get("geometry"), dict) else {}
        points = geometry.get("coordinates", [])
        valid_geometry = (geometry.get("source") == "amap_route_steps" and geometry.get("coordinate_system") == "GCJ-02"
                          and isinstance(points, list) and 2 <= len(points) <= 1500 and all(point(p) for p in points))
        if not valid_geometry:
            points = []
        freshness = result.get("_freshness") if isinstance(result.get("_freshness"), dict) else {
            "issued_at": row["updated_at"],
            "expires_at": (datetime.fromisoformat(row["updated_at"]) + timedelta(seconds=route_seconds)).isoformat()}
        steps = result.get("steps") if isinstance(result.get("steps"), list) else []
        try:
            plan = json.loads(row.get("task_plan_json") or "{}")
            names = plan.get("slots", {}) if isinstance(plan, dict) and plan.get("action") == "navigation_route" else {}
            if not isinstance(names, dict):
                names = {}
        except (TypeError, ValueError):
            names = {}
        record = {"task_id": row["task_id"], "root_task_id": row.get("parent_task_id") or row["task_id"],
                "session_id": row["session_id"], "job_id": row["id"], "query": row["query"],
                "origin": origin, "destination": destination,
                "origin_name": str(result.get("origin_name") or names.get("origin_name") or "起点")[:200],
                "destination_name": str(result.get("destination_name") or names.get("destination_name") or "目的地")[:200],
                "travel_mode": mode, "distance_m": distance, "duration_s": duration,
                "default_travel_mode": mode, "mode_choices": mode_choices(mode),
                "tolls": str(result.get("tolls", "0"))[:30],
                "steps": [item for item in steps[:80] if isinstance(item, str) and item.strip()],
                "step_count": int(result.get("step_count", len(steps))),
                "steps_truncated": bool(result.get("steps_truncated")), "geometry_available": bool(points),
                "geometry": {"coordinates": points}, "source": "高德地图", "issued_at": freshness.get("issued_at"),
                "expires_at": freshness.get("expires_at"), "is_stale": expired(freshness),
                "navigation_url": "https://uri.amap.com/navigation?" + urlencode({"from": origin, "to": destination,
                    "mode": {"driving": "car", "walking": "walk", "bicycling": "ride"}[mode],
                    "src": "LifePlus", "coordinate": "gaode", "callnative": "1"})}
        record["map_zoom"] = int(static_map_parameters(record)["zoom"])
        record["map_zoom_large"] = int(static_map_parameters(record, large=True)["zoom"])
        record["option_id"] = 0
        record["options"] = [{"option_id": 0, "label": "方案 1", "distance_m": distance, "duration_s": duration,
                              "tolls": record["tolls"], "strategy": "", "geometry_available": bool(points)}]
        return record
    except (KeyError, ValueError, TypeError, OverflowError):
        return None


async def route_rows(db, *, owner: str, session_id: str | None = None, task_id: str | None = None) -> list[dict]:
    field, identifier = ("t.session_id", session_id) if session_id else ("t.id", task_id)
    return await db.fetchall(
        "SELECT j.*,t.session_id,t.parent_task_id,t.query,t.plan_json AS task_plan_json FROM tool_jobs j JOIN tasks t ON t.id=j.task_id "
        f"WHERE t.user_id=? AND {field}=? AND t.status!='cancelled' AND j.tool_name='maps_route' AND j.status='completed' "
        "ORDER BY j.created_at DESC,j.rowid DESC", (owner, identifier))


async def stored_route(db, owner: str, task_id: str, job_id: str | None = None, route_seconds: float = 120,
                       option: int | None = None) -> dict:
    for row in await route_rows(db, owner=owner, task_id=task_id):
        if job_id and row["id"] != job_id:
            continue
        route = route_record(row, route_seconds, option)
        if route:
            # Bind the display name to actual POI evidence rather than guessing from the user query.
            if route["destination_name"] == "目的地":
                evidence = await db.fetchall(
                    "SELECT result_json FROM tool_jobs WHERE task_id=? AND tool_name IN ('maps_search','maps_place_detail','maps_local_search') "
                    "AND status='completed' AND created_at<=? ORDER BY created_at DESC", (task_id, row["created_at"]))
                for item in evidence:
                    try:
                        result = json.loads(item["result_json"] or "{}")
                    except ValueError:
                        continue
                    if not isinstance(result, dict):
                        continue
                    candidates = result.get("items") or ([result["item"]] if result.get("item") else [])
                    if not isinstance(candidates, list):
                        continue
                    match = next((candidate for candidate in candidates if isinstance(candidate, dict) and point(candidate.get("location"))
                                  and coordinate_text(candidate["location"]) == route["destination"]), None)
                    if match:
                        route["destination_name"] = str(match.get("name") or "目的地")[:200]
                        break
            return route
    raise HTTPException(404, "没有找到当前用户可查看的真实路线回执")


def static_map_parameters(route: dict, zoom_delta: int = 0, large: bool = False) -> dict:
    points = route.get("geometry", {}).get("coordinates") or [point(route["origin"]), point(route["destination"])]
    width, height = (1000, 600) if large else (800, 400)
    fitting = [point(route["origin"]), *points, point(route["destination"])]
    xs = [(p[0] + 180) / 360 for p in fitting]
    ys = [(1 - math.asinh(math.tan(math.radians(p[1]))) / math.pi) / 2 for p in fitting]
    dx, dy = max(xs) - min(xs), max(ys) - min(ys)
    # AMap static-map framing is calibrated against its returned images, not a 256px slippy tile.
    zoom = min(17, max(1, math.floor(min(math.log2((width - 120) / (512 * max(dx, 1e-9))),
                                         math.log2((height - 120) / (512 * max(dy, 1e-9)))))))
    center_x, center_y = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2
    center = [center_x * 360 - 180, math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * center_y))))]
    params = {"size": f"{width}*{height}", "zoom": str(min(17, max(1, zoom + zoom_delta))),
              "location": coordinate_text(center),
              "markers": f"mid,0x16836b,起:{route['origin']}|mid,0xe15b52,终:{route['destination']}"}
    if route.get("geometry_available"):
        encoded = ";".join(coordinate_text(p) for p in sample_points(points, 100))
        params["paths"] = "6,0x237ce6,1,,:" + encoded
    return params


class MapPreviewError(Exception):
    pass


class RouteMapRenderer:
    def __init__(self, key: str | None, transport=None):
        self.key = key
        self.transport = transport
        self.cache = OrderedDict()
        self.lock = asyncio.Lock()
        self.slots = asyncio.Semaphore(3)

    async def render(self, route: dict, zoom_delta: int = 0, large: bool = False) -> bytes:
        if not self.key:
            raise MapPreviewError("未配置高德地图服务")
        params = static_map_parameters(route, zoom_delta, large)
        cache_key = (route["job_id"], tuple(sorted(params.items())))
        async with self.lock:
            cached = self.cache.get(cache_key)
            if cached and cached[0] > monotonic():
                self.cache.move_to_end(cache_key)
                return cached[1]
        try:
            async with asyncio.timeout(12), self.slots:
                async with httpx.AsyncClient(timeout=8, transport=self.transport, follow_redirects=False) as client:
                    async with client.stream("GET", "https://restapi.amap.com/v3/staticmap", params={**params, "key": self.key}) as response:
                        if response.status_code != 200:
                            raise MapPreviewError("高德地图画面暂不可用，请重试")
                        image = bytearray()
                        async for chunk in response.aiter_bytes():
                            image.extend(chunk)
                            if len(image) > 3_000_000:
                                raise MapPreviewError("地图图片超出大小限制")
                        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
                            raise MapPreviewError("高德未返回地图图片，请检查静态地图服务权限或配额")
        except (httpx.HTTPError, TimeoutError) as exc:
            raise MapPreviewError("地图加载超时或网络不可用，路线信息仍可查看") from exc
        data = bytes(image)
        async with self.lock:
            self.cache[cache_key] = (monotonic() + 120, data)
            self.cache.move_to_end(cache_key)
            while len(self.cache) > 24:
                self.cache.popitem(last=False)
        return data
