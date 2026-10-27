from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

import aiosqlite
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ..route_geometry import geometry_from_steps


Text = Annotated[str, Field(min_length=1, max_length=500)]


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class WeatherArgs(Arguments):
    city: Text
    forecast_date: str = ""


class SearchArgs(Arguments):
    keywords: Text
    city: str = Field(default="", max_length=100)


class RouteArgs(Arguments):
    origin: Text
    destination: Text
    city: str = Field(default="", max_length=100)
    travel_mode: Literal["driving", "walking", "bicycling"] = "driving"


LOCAL_CATEGORIES = {"restaurant": ("饭店", "050100|050200|050300|050400"), "ktv": ("KTV", "080302"),
                    "cinema": ("电影院", "080601"), "cafe": ("咖啡店", "050500")}


class LocalSearchArgs(Arguments):
    category: Literal["restaurant", "ktv", "cinema", "cafe"]
    anchor: Text
    city: str = Field(default="", max_length=100)
    keyword: str = Field(default="", max_length=100)
    radius_m: int = Field(default=3000, ge=200, le=50000)
    sort_by: Literal["distance", "rating"] = "distance"
    min_rating: float | None = Field(default=None, ge=0, le=5)
    max_cost: float | None = Field(default=None, gt=0, le=10000)
    limit: int = Field(default=6, ge=1, le=10)


class PlaceDetailArgs(Arguments):
    poi_id: Text


class LocalRouteArgs(PlaceDetailArgs):
    anchor: Text
    travel_mode: Literal["driving", "walking", "bicycling"] = "driving"


class MusicArgs(Arguments):
    keyword: Text
    limit: int = Field(default=5, ge=1, le=10)


class UserArgs(Arguments):
    user_id: Annotated[str, Field(min_length=1, max_length=255)]


class WriteArgs(UserArgs):
    title: Text
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200)]


class ItineraryArgs(WriteArgs):
    details: dict[str, Any] = Field(default_factory=dict)


class ReminderArgs(WriteArgs):
    remind_at: Text


class CancelReminderArgs(UserArgs):
    reminder_id: Text


TOOL_CATALOG = {
    "maps_weather": (WeatherArgs, "高德城市天气预报", False, 10),
    "maps_search": (SearchArgs, "高德地点检索", False, 10),
    "maps_route": (RouteArgs, "高德驾车、步行和骑行路线", False, 15),
    "maps_local_search": (LocalSearchArgs, "按参考地点和分类检索真实饭店、KTV、电影院、咖啡店；未知评分价格不猜测", False, 15),
    "maps_place_detail": (PlaceDetailArgs, "通过真实 POI ID 查询商家详情、照片与联系方式；不包含订票预订", False, 10),
    "search_music": (MusicArgs, "QQ音乐歌曲检索及官方详情链接", False, 10),
    "save_itinerary": (ItineraryArgs, "确认后保存行程", True, 5),
    "create_reminder": (ReminderArgs, "确认后创建到期站内提醒", True, 5),
    "list_itineraries": (UserArgs, "读取用户行程", False, 5),
    "list_reminders": (UserArgs, "读取用户提醒及触发状态", False, 5),
    "cancel_reminder": (CancelReminderArgs, "取消未触发的提醒", True, 5),
}


class ToolError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, **details: Any):
        super().__init__(message)
        self.code, self.message, self.retryable, self.details = code, message, retryable, details


def normalize_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError("INVALID_ARGUMENT", "提醒时间必须为带时区的 ISO-8601 日期时间") from exc
    if parsed.tzinfo is None:
        raise ToolError("INVALID_ARGUMENT", "提醒时间缺少时区，请明确 +08:00 等时区")
    return parsed.astimezone(timezone.utc).isoformat()


def location_query(value: str, city: str, field: str) -> tuple[str, str]:
    value = value.strip()
    if field == "origin":
        match = re.fullmatch(r"(?:请|帮我)?\s*(?:从|由)\s*(.+?)\s*(?:出发|启程)", value)
        if match:
            value = match.group(1).strip()
    # The shared city hint may belong to the destination of a cross-city route.
    explicit_city = re.search(r"(?:^|省|自治区)([^省市区县\s,，]{2,12}市)", value)
    if city and explicit_city and explicit_city.group(1).removesuffix("市") != city.removesuffix("市"):
        city = ""
    return value, city


def coordinate(value: str, field: str) -> str | None:
    if not re.fullmatch(r"-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?", value):
        return None
    lng, lat = map(float, value.split(","))
    if not (math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90):
        raise ToolError("INVALID_ARGUMENT", "经纬度超出合法范围", field=field)
    return ",".join(f"{number:.6f}".rstrip("0").rstrip(".") if number else "0" for number in (lng, lat))


def optional_text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def optional_number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    except (TypeError, ValueError):
        return None


def venue_item(row: dict) -> dict:
    business = row.get("biz_ext") if isinstance(row.get("biz_ext"), dict) else {}
    photos = [{"url": photo["url"], "title": optional_text(photo.get("title"))}
              for photo in (row.get("photos") or []) if isinstance(photo, dict)
              and isinstance(photo.get("url"), str) and photo["url"].startswith(("https://", "http://"))][:4]
    location = optional_text(row.get("location"))
    name = optional_text(row.get("name")) or "未命名商家"
    return {"id": row.get("id"), "name": name,
            "address": optional_text(row.get("address")), "location": location,
            "city": optional_text(row.get("cityname")), "district": optional_text(row.get("adname")),
            "type": optional_text(row.get("type")), "distance_m": optional_number(row.get("distance")),
            "rating": optional_number(business.get("rating")), "cost": optional_number(business.get("cost")),
            "phone": optional_text(row.get("tel")),
            "availability": "reported_closed" if re.search(r"暂停营业|已停业|已关闭|歇业", name) else "unknown",
            "opening_hours": optional_text(business.get("opentime")) or optional_text(row.get("opentime")),
            "photos": photos, "map_url": "https://uri.amap.com/marker?" + urlencode({"position": location, "name": row.get("name", "")}) if location else None}


class LifeTools:
    """Real upstream queries and transactional local writes; never synthesizes results."""

    def __init__(self, database_path: str, amap_key: str | None = None,
                 http_transport: httpx.AsyncBaseTransport | None = None):
        self.database_path = database_path
        self.amap_key = amap_key
        self.http_transport = http_transport

    def catalog(self) -> list[dict[str, Any]]:
        return [{"name": name, "description": description, "side_effect": write,
                 "timeout_seconds": timeout, "input_schema": model.model_json_schema(),
                 "implementation": "real"}
                for name, (model, description, write, timeout) in TOOL_CATALOG.items()]

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        started = perf_counter()
        try:
            if name not in TOOL_CATALOG:
                raise ToolError("UNKNOWN_TOOL", "工具未注册")
            values = TOOL_CATALOG[name][0].model_validate(arguments).model_dump()
            result = await getattr(self, name)(**values)
            return {"ok": True, **result, "elapsed_ms": round((perf_counter() - started) * 1000)}
        except ValidationError as exc:
            fields = [".".join(map(str, error["loc"])) for error in exc.errors()]
            result = {"error": "工具参数不合法：" + ", ".join(fields),
                      "error_code": "INVALID_ARGUMENT", "retryable": False}
        except ToolError as exc:
            result = {"error": exc.message, "error_code": exc.code,
                      "retryable": exc.retryable, **exc.details}
        except httpx.TimeoutException:
            result = {"error": "外部服务请求超时", "error_code": "UPSTREAM_TIMEOUT", "retryable": True}
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            result = {"error": f"外部服务 HTTP {status}", "error_code": "UPSTREAM_HTTP",
                      "retryable": status == 429 or status >= 500}
            try:
                result["retry_after_seconds"] = min(3600.0, max(0.0, float(exc.response.headers["Retry-After"])))
            except (KeyError, TypeError, ValueError):
                pass
        except httpx.RequestError:
            result = {"error": "无法连接外部服务", "error_code": "UPSTREAM_NETWORK", "retryable": True}
        except aiosqlite.Error:
            result = {"error": "本地工具存储暂不可用", "error_code": "STORAGE_ERROR", "retryable": True}
        except (ValueError, TypeError, KeyError):
            result = {"error": "外部服务返回格式异常", "error_code": "UPSTREAM_SCHEMA", "retryable": False}
        return {"ok": False, **result, "elapsed_ms": round((perf_counter() - started) * 1000)}

    async def _get(self, url: str, params: dict) -> dict:
        async with httpx.AsyncClient(timeout=8, transport=self.http_transport) as client:
            response = await client.get(url, params=params, headers={"Referer": "https://y.qq.com/"})
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict):
            raise ToolError("UPSTREAM_SCHEMA", "外部服务没有返回 JSON 对象")
        return payload

    async def _amap(self, path: str, **params: Any) -> dict:
        if not self.amap_key:
            raise ToolError("NOT_CONFIGURED", "缺少 AMAP_MAPS_API_KEY")
        payload = await self._get("https://restapi.amap.com/" + path,
                                  {"key": self.amap_key, **params})
        if path.startswith("v4/"):
            valid = str(payload.get("errcode")) == "0"
            code = str(payload.get("errcode", "UNKNOWN"))
        else:
            valid = str(payload.get("status")) == "1"
            code = str(payload.get("infocode", "UNKNOWN"))
        if not valid:
            engine_failure = code.startswith("300")
            transient = engine_failure or code in {"10003", "10004", "10014", "10016", "10021", "10044", "10045"}
            stage = "地址解析" if path == "v3/geocode/geo" else "路线规划" if "/direction/" in path else "查询"
            message = (f"高德{stage}服务响应失败，请核对地点和城市后重试（错误码 {code}）" if engine_failure
                       else "高德服务拒绝请求（错误码 " + code + "）")
            raise ToolError("AMAP_" + code, message, transient,
                            upstream_path=path, upstream_code=code, upstream_info=str(payload.get("info") or payload.get("errmsg") or "")[:120])
        return payload

    async def maps_weather(self, city: str, forecast_date: str = "") -> dict:
        try:
            target = date.fromisoformat(forecast_date) if forecast_date else datetime.now(timezone(timedelta(hours=8))).date()
        except ValueError as exc:
            raise ToolError("INVALID_ARGUMENT", "forecast_date 必须为 YYYY-MM-DD") from exc
        payload = await self._amap("v3/weather/weatherInfo", city=city, extensions="all")
        forecasts = payload.get("forecasts") or []
        if not forecasts:
            raise ToolError("NO_RESULTS", "没有找到该城市的天气，请补充城市名称")
        forecast = forecasts[0]
        casts = forecast.get("casts", [])
        item = next((row for row in casts if row.get("date") == target.isoformat()), None)
        if not item:
            raise ToolError("DATE_UNAVAILABLE", "该日期不在高德可查询的预报范围内",
                            available_dates=[row["date"] for row in casts])
        return {"city": forecast.get("city", city), "date": target.isoformat(),
                "day_weather": item.get("dayweather"), "night_weather": item.get("nightweather"),
                "low": item.get("nighttemp"), "high": item.get("daytemp"),
                "wind": item.get("daywind"), "source": "高德天气"}

    async def maps_search(self, keywords: str, city: str = "") -> dict:
        payload = await self._amap("v3/place/text", keywords=keywords, city=city,
                                   offset=5, extensions="base")
        return {"items": [{"id": item.get("id"), "name": item.get("name"),
                           "address": item.get("address") or "", "location": item.get("location"),
                           "city": item.get("cityname") or ""} for item in payload.get("pois", [])],
                "source": "高德地图"}

    async def maps_local_search(self, category: str, anchor: str, city: str = "", keyword: str = "",
                                radius_m: int = 3000, sort_by: str = "distance", min_rating: float | None = None,
                                max_cost: float | None = None, limit: int = 6) -> dict:
        center = await self._location(anchor, city, "anchor")
        payload = await self._amap("v3/place/around", location=center, types=LOCAL_CATEGORIES[category][1],
                                   keywords=keyword, radius=radius_m, sortrule="distance", offset=20, page=1, extensions="all")
        items = [venue_item(row) for row in payload.get("pois", []) if row.get("id") and row.get("location")]
        missing_rating = sum(item["rating"] is None for item in items)
        missing_cost = sum(item["cost"] is None for item in items)
        filtered = [item for item in items
                    if item["availability"] != "reported_closed"
                    and (min_rating is None or item["rating"] is not None and item["rating"] >= min_rating)
                    and (max_cost is None or item["cost"] is not None and item["cost"] <= max_cost)
                    and (item["distance_m"] is None or item["distance_m"] <= radius_m)]
        filtered.sort(key=(lambda item: (item["rating"] is None, -(item["rating"] or 0), item["distance_m"] or 0))
                      if sort_by == "rating" else lambda item: (item["distance_m"] is None, item["distance_m"] or 0))
        return {"items": filtered[:limit], "center": center, "anchor": anchor, "category": category,
                "radius_m": radius_m, "sort_by": sort_by, "source": "高德地图",
                "candidate_count": len(items), "matched_count": len(filtered),
                "missing_rating_count": missing_rating, "missing_cost_count": missing_cost,
                "excluded_closed_count": sum(item["availability"] == "reported_closed" for item in items),
                "constraints": {"min_rating": min_rating, "max_cost": max_cost, "keyword": keyword},
                "ranking_scope": "高德按距离返回的首批最多20个候选内排序筛选，不代表全城排名",
                "booking_supported": False, "realtime_availability_supported": False}

    async def maps_place_detail(self, poi_id: str) -> dict:
        payload = await self._amap("v3/place/detail", id=poi_id, extensions="all")
        rows = payload.get("pois") or []
        if not rows:
            raise ToolError("NO_RESULTS", "未找到该商家详情，可能已下线或 POI ID 已失效")
        item = venue_item(rows[0])
        if item["id"] != poi_id:
            raise ToolError("UPSTREAM_SCHEMA", "返回商家与请求 POI ID 不一致")
        return {"item": item, "source": "高德地图", "booking_supported": False,
                "realtime_availability_supported": False,
                "notice": "评分、人均与营业时间仅以高德返回为准；未提供的字段未知，不代表实时营业、余位或电影排期"}

    async def _location(self, value: str, city: str, field: str) -> str:
        value, city = location_query(value, city, field)
        normalized = coordinate(value, field)
        if normalized is not None:
            return normalized
        params = {"address": value}
        if city:
            params["city"] = city
        payload = await self._amap("v3/geocode/geo", **params)
        candidates = payload.get("geocodes") or []
        if len(candidates) != 1:
            raise ToolError("AMBIGUOUS_LOCATION" if candidates else "NO_RESULTS",
                            "请补充更明确的起点或终点", field=field, requires_clarification=True,
                            candidates=[{"name": row.get("formatted_address"), "location": row.get("location")}
                                        for row in candidates])
        location = candidates[0].get("location")
        if not location:
            raise ToolError("UPSTREAM_SCHEMA", "地址解析结果缺少坐标")
        try:
            normalized = coordinate(location, field)
        except ToolError as exc:
            raise ToolError("UPSTREAM_SCHEMA", "地址解析结果坐标不合法", field=field) from exc
        if normalized is None:
            raise ToolError("UPSTREAM_SCHEMA", "地址解析结果坐标格式不合法", field=field)
        return normalized

    async def maps_route(self, origin: str, destination: str, city: str = "",
                         travel_mode: str = "driving") -> dict:
        origin_query, origin_city = location_query(origin, city, "origin")
        destination_query, destination_city = location_query(destination, city, "destination")
        origin_location, destination_location = await asyncio.gather(
            self._location(origin_query, origin_city, "origin"), self._location(destination_query, destination_city, "destination"))
        path = "v4/direction/bicycling" if travel_mode == "bicycling" else f"v3/direction/{travel_mode}"
        params = {"origin": origin_location, "destination": destination_location}
        if travel_mode == "driving":
            params.update(strategy="10", extensions="all")
        payload = await self._amap(path, **params)
        route = payload.get("data", {}) if travel_mode == "bicycling" else payload.get("route", {})
        paths = route.get("paths") or []
        if not paths:
            raise ToolError("NO_ROUTE", "没有可用路线，请调整地点或出行方式")
        alternatives = []
        for index, candidate in enumerate(paths[:5]):
            if not isinstance(candidate, dict):
                continue
            try:
                distance, duration = float(candidate["distance"]), float(candidate["duration"])
            except (KeyError, ValueError, TypeError):
                continue
            if not all(math.isfinite(value) and value > 0 for value in (distance, duration)):
                continue
            steps = candidate.get("steps") or []
            if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
                continue
            alternatives.append({"option_id": index, "strategy": str(candidate.get("strategy") or "")[:100],
                "distance_m": distance, "duration_s": duration, "tolls": candidate.get("tolls", "0"),
                "steps": [step.get("instruction", "") for step in steps[:80]], "step_count": len(steps),
                "steps_truncated": len(steps) > 80, "geometry": geometry_from_steps(steps)})
        if not alternatives:
            raise ToolError("UPSTREAM_SCHEMA", "高德返回的路线缺少有效距离或耗时")
        selected = alternatives[0]
        return {"travel_mode": travel_mode, "origin": origin_location, "destination": destination_location,
                "origin_name": origin_query if coordinate(origin_query, "origin") is None else "",
                "destination_name": destination_query if coordinate(destination_query, "destination") is None else "",
                **selected, "alternatives": alternatives, "alternatives_truncated": len(paths) > 5,
                "navigation_url": "https://uri.amap.com/navigation?" + urlencode({
                    "from": origin_location, "to": destination_location,
                    "mode": {"driving": "car", "walking": "walk", "bicycling": "ride"}[travel_mode],
                    "src": "LifePlus", "coordinate": "gaode", "callnative": "0"}),
                "source": "高德地图"}

    async def search_music(self, keyword: str, limit: int = 5) -> dict:
        payload = await self._get("https://c.y.qq.com/splcloud/fcgi-bin/smartbox_new.fcg",
                                  {"key": keyword, "format": "json"})
        if payload.get("code") != 0:
            raise ToolError("MUSIC_UPSTREAM", "QQ音乐检索暂不可用", retryable=True)
        rows = (payload.get("data") or {}).get("song", {}).get("itemlist", [])
        items = [{"id": str(row["id"]), "mid": row.get("mid", ""), "name": row["name"],
                  "singer": row.get("singer", ""), "album": row.get("albumname", ""),
                  "url": "https://y.qq.com/n/ryqq/songDetail/" + row["mid"]}
                 for row in rows[:limit] if row.get("id") and row.get("mid") and row.get("name")]
        return {"items": items, "source": "QQ音乐", "search_type": "quick_search",
                "returned_count": len(items), "playback_supported": False}

    async def _connection(self) -> aiosqlite.Connection:
        Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)
        connection = await aiosqlite.connect(self.database_path, timeout=5)
        connection.row_factory = aiosqlite.Row
        from ..persistence import SCHEMA

        await connection.executescript(SCHEMA)
        return connection

    async def _write(self, name: str, user_id: str, title: str, idempotency_key: str,
                     details: dict | None = None, remind_at: str = "") -> dict:
        canonical = json.dumps({"title": title, "details": details, "remind_at": remind_at},
                               ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()
        resource_id = hashlib.sha256(json.dumps([name, user_id, idempotency_key]).encode()).hexdigest()
        connection = await self._connection()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            row = await (await connection.execute(
                "SELECT payload_hash,result_json FROM tool_receipts WHERE tool_name=? AND user_id=? AND idempotency_key=?",
                (name, user_id, idempotency_key))).fetchone()
            if row:
                if row["payload_hash"] != digest:
                    raise ToolError("IDEMPOTENCY_CONFLICT", "相同幂等键对应的参数已发生变化")
                return {**json.loads(row["result_json"]), "deduplicated": True}
            now = datetime.now(timezone.utc).isoformat()
            if name == "create_reminder":
                if datetime.fromisoformat(remind_at) <= datetime.now(timezone.utc):
                    raise ToolError("INVALID_ARGUMENT", "提醒时间必须晚于当前时间")
                await connection.execute("INSERT INTO reminders VALUES(?,?,?,?,?,?)",
                                         (resource_id, user_id, title, remind_at, "scheduled", now))
                result = {"status": "scheduled", "reminder_id": resource_id, "title": title,
                          "remind_at": remind_at, "delivery_channel": "in_app", "source": "LifePlus"}
            else:
                await connection.execute("INSERT INTO itinerary_items VALUES(?,?,?,?,?)",
                                         (resource_id, user_id, title, json.dumps(details or {}, ensure_ascii=False), now))
                result = {"status": "saved", "item_id": resource_id, "title": title,
                          "details": details or {}, "source": "LifePlus"}
            await connection.execute("INSERT INTO tool_receipts VALUES(?,?,?,?,?,?)",
                                     (name, user_id, idempotency_key, digest, json.dumps(result, ensure_ascii=False), now))
            await connection.commit()
            return result
        finally:
            await connection.rollback()
            await connection.close()

    async def save_itinerary(self, user_id: str, title: str, idempotency_key: str,
                             details: dict | None = None) -> dict:
        return await self._write("save_itinerary", user_id, title, idempotency_key, details=details or {})

    async def create_reminder(self, user_id: str, title: str, remind_at: str, idempotency_key: str) -> dict:
        return await self._write("create_reminder", user_id, title, idempotency_key,
                                 remind_at=normalize_time(remind_at))

    async def _list(self, table: str, user_id: str) -> dict:
        connection = await self._connection()
        try:
            rows = await (await connection.execute(
                f"SELECT * FROM {table} WHERE user_id=? ORDER BY created_at DESC LIMIT 100", (user_id,))).fetchall()
            return {"items": [dict(row) for row in rows], "source": "LifePlus"}
        finally:
            await connection.close()

    async def list_itineraries(self, user_id: str) -> dict:
        return await self._list("itinerary_items", user_id)

    async def list_reminders(self, user_id: str) -> dict:
        return await self._list("reminders", user_id)

    async def cancel_reminder(self, user_id: str, reminder_id: str) -> dict:
        connection = await self._connection()
        try:
            await connection.execute("BEGIN IMMEDIATE")
            row = await (await connection.execute("SELECT status FROM reminders WHERE id=? AND user_id=?",
                                                  (reminder_id, user_id))).fetchone()
            if not row:
                raise ToolError("NOT_FOUND", "提醒不存在")
            if row["status"] == "delivered":
                raise ToolError("ALREADY_DELIVERED", "提醒已经触发")
            await connection.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND user_id=?",
                                     (reminder_id, user_id))
            await connection.commit()
            return {"status": "cancelled", "reminder_id": reminder_id, "source": "LifePlus"}
        finally:
            await connection.rollback()
            await connection.close()
