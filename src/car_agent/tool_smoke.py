"""Real MCP integration checks. Run: python -m car_agent.tool_smoke --output data/tool-report.json"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any

from .adapters.mcp_client import LocalMCPClient
from .config import get_settings
from .persistence import Database
from .reminders import ReminderScheduler
from .tools import TOOL_CATALOG


async def run_checks(include_routes: bool = True) -> dict[str, Any]:
    settings = get_settings()
    cases: list[dict[str, Any]] = []
    with TemporaryDirectory(prefix="lifeplus-live-tools-") as temporary:
        database_path = str(Path(temporary) / "tools.sqlite3")
        environment = dict(os.environ)
        environment["LIFEPLUS_DATA_PATH"] = database_path
        if settings.amap_maps_api_key:
            environment["AMAP_MAPS_API_KEY"] = settings.amap_maps_api_key
        client = LocalMCPClient(environment)
        db = Database(database_path)
        await db.open()
        scheduler = ReminderScheduler(db, interval=0.1)

        async def call(label: str, name: str, arguments: dict,
                       expected_error: str | None = None) -> dict:
            start = perf_counter()
            try:
                result = await client.call(name, arguments)
                valid = result.get("error_code") == expected_error if expected_error else result.get("ok") is True
                if result.get("simulated"):
                    valid = False
                cases.append({"case": label, "tool": name, "passed": valid,
                              "elapsed_ms": round((perf_counter() - start) * 1000), "response": result})
                return result
            except Exception as exc:
                cases.append({"case": label, "tool": name, "passed": False,
                              "error_type": type(exc).__name__,
                              "elapsed_ms": round((perf_counter() - start) * 1000)})
                return {}

        try:
            await client.open()
            cases.append({"case": "mcp_discovery", "passed": set(TOOL_CATALOG) <= client.tool_names,
                          "tools": sorted(client.tool_names)})
            weather = await call("weather_live", "maps_weather", {"city": "无锡"})
            cases[-1]["passed"] &= bool(weather.get("day_weather") and weather.get("source") == "高德天气")
            places = await call("poi_live", "maps_search", {"keywords": "东南大学无锡校区", "city": "无锡"})
            cases[-1]["passed"] &= bool(places.get("items") and places["items"][0].get("location"))
            for mode in (("driving", "walking", "bicycling") if include_routes else ()):
                route = await call("route_" + mode, "maps_route", {
                    "origin": "120.1482,30.2395", "destination": "120.1551,30.2420",
                    "city": "杭州", "travel_mode": mode,
                })
                cases[-1]["passed"] &= bool(route.get("distance_m", 0) > 0 and route.get("duration_s", 0) > 0)
            if include_routes:
                recovered = await call("route_legacy_30001_cross_city", "maps_route", {
                    "origin": "从无锡市人民医院出发", "destination": "118.795193,32.054213",
                    "city": "南京市", "travel_mode": "driving"})
                cases[-1]["passed"] &= bool(recovered.get("origin") == "120.324003,31.542815"
                                                and recovered.get("distance_m", 0) > 0)
            music = await call("music_live", "search_music", {"keyword": "周杰伦", "limit": 3})
            cases[-1]["passed"] &= bool(music.get("items") and music["items"][0].get("url"))
            for category in ("restaurant", "ktv", "cinema", "cafe"):
                nearby = await call("local_" + category, "maps_local_search", {
                    "category": category, "anchor": "东南大学无锡国际校区", "city": "无锡市", "radius_m": 5000, "limit": 3})
                cases[-1]["passed"] &= bool(nearby.get("items") and nearby.get("center"))
                if nearby.get("items"):
                    venue = nearby["items"][0]
                    detail = await call("detail_" + category, "maps_place_detail", {"poi_id": venue["id"]})
                    cases[-1]["passed"] &= detail.get("item", {}).get("id") == venue["id"]
            started = perf_counter()
            reads = await asyncio.gather(
                client.call("maps_weather", {"city": "杭州"}),
                client.call("search_music", {"keyword": "陶喆", "limit": 2}),
                return_exceptions=True,
            )
            cases.append({"case": "concurrent_real_mcp_reads",
                          "passed": all(isinstance(result, dict) and result.get("ok") for result in reads),
                          "elapsed_ms": round((perf_counter() - started) * 1000),
                          "sources": [result.get("source") for result in reads if isinstance(result, dict)]})

            itinerary_arguments = {"user_id": "live-test", "title": "无锡校园行程",
                                   "details": {"destination": "东南大学无锡校区"}, "idempotency_key": "itinerary-1"}
            saved = await call("itinerary_write", "save_itinerary", itinerary_arguments)
            replayed = await call("itinerary_idempotency", "save_itinerary", itinerary_arguments)
            cases[-1]["passed"] &= bool(replayed.get("deduplicated") and saved.get("item_id") == replayed.get("item_id"))
            await call("itinerary_conflict", "save_itinerary",
                       {**itinerary_arguments, "title": "不相同的行程"}, "IDEMPOTENCY_CONFLICT")
            listed = await call("itinerary_read", "list_itineraries", {"user_id": "live-test"})
            cases[-1]["passed"] &= len(listed.get("items", [])) == 1
            isolated = await call("user_isolation", "list_itineraries", {"user_id": "other-user"})
            cases[-1]["passed"] &= isolated.get("items") == []

            due = await call("reminder_write", "create_reminder", {
                "user_id": "live-test", "title": "真实到期测试",
                "remind_at": (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat(),
                "idempotency_key": "reminder-due",
            })
            cancelled = await call("reminder_cancel_target", "create_reminder", {
                "user_id": "live-test", "title": "应被取消的提醒",
                "remind_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                "idempotency_key": "reminder-cancel",
            })
            await call("reminder_cancel", "cancel_reminder", {
                "user_id": "live-test", "reminder_id": cancelled.get("reminder_id", "unknown")})
            await scheduler.start()
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 5
            delivered = None
            while loop.time() < deadline:
                delivered = await db.fetchone("SELECT * FROM notifications WHERE reminder_id=?",
                                              (due.get("reminder_id", "unknown"),))
                if delivered:
                    break
                await asyncio.sleep(0.1)
            await scheduler.tick()
            notification_count = await db.fetchone("SELECT COUNT(*) AS count FROM notifications")
            cases.append({"case": "real_due_delivery_once", "passed": bool(delivered) and notification_count["count"] == 1,
                          "channel": "in_app", "notification_count": notification_count["count"]})
            reminders = await call("reminder_read", "list_reminders", {"user_id": "live-test"})
            states = {item["id"]: item["status"] for item in reminders.get("items", [])}
            cases[-1]["passed"] &= (states.get(due.get("reminder_id")) == "delivered"
                                         and states.get(cancelled.get("reminder_id")) == "cancelled")
            await call("reminder_invalid_time", "create_reminder", {
                "user_id": "live-test", "title": "无效时间",
                "remind_at": "明天", "idempotency_key": "invalid-time",
            }, "INVALID_ARGUMENT")
        finally:
            await scheduler.close()
            await client.close()
            await db.close()

    return {"tested_at": datetime.now(timezone.utc).isoformat(), "transport": "MCP stdio",
            "external_services": ["高德地图", "QQ音乐"], "writes": "isolated real SQLite database",
            "skipped": [] if include_routes else [{"case": "route_" + mode, "reason": "显式跳过路线服务联调；不修改待排查的高德请求失败逻辑"}
                                                   for mode in ("driving", "walking", "bicycling")],
            "passed": sum(bool(case["passed"]) for case in cases), "total": len(cases), "cases": cases}


def main() -> None:
    parser = argparse.ArgumentParser(description="LifePlus 真实工具集成测试（需要高德Key，无模拟回退）")
    parser.add_argument("--output", type=Path, default=Path("data/tool-integration-latest.json"))
    parser.add_argument("--skip-routes", action="store_true", help="跳过三个真实路线请求，其他工具照常实际调用")
    arguments = parser.parse_args()
    report = asyncio.run(run_checks(include_routes=not arguments.skip_routes))
    arguments.output.parent.mkdir(parents=True, exist_ok=True)
    arguments.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for case in report["cases"]:
        print(f"{'PASS' if case['passed'] else 'FAIL'} {case['case']}: {case.get('elapsed_ms', '-')} ms", flush=True)
    print(f"{report['passed']}/{report['total']} passed; report: {arguments.output}")
    raise SystemExit(0 if report["passed"] == report["total"] else 1)


if __name__ == "__main__":
    main()
