"""Read-only upstream connection pooling and controlled delta-storage tests."""
from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import httpx

from car_agent.config import Settings
from car_agent.persistence import Database
from car_agent.tools.service import LifeTools, ToolError
from evaluations.speed_common import begin, finish, paired


class PooledTools(LifeTools):
    """Experiment subclass only: the installed MCP server is not changed."""
    def __init__(self, *args, client, **kwargs):
        super().__init__(*args, **kwargs)
        self.client = client

    async def _get(self, url, params):
        response = await self.client.get(url, params=params, headers={"Referer": "https://y.qq.com/"})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ToolError("UPSTREAM_SCHEMA", "外部服务没有返回 JSON 对象")
        return payload


def valid_tool(name, result):
    if result.get("ok") is not True:
        return False
    if name == "maps_weather":
        return all(result.get(key) for key in ("city", "date", "day_weather", "source"))
    return bool(result.get("items")) and all(item.get("id") and item.get("location") for item in result["items"])


async def pool(args):
    settings = Settings()
    today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    cases = [{"id": f"weather-{city}-{repeat}", "tool": "maps_weather", "args": {"city": city, "forecast_date": today}}
             for city in ("无锡", "杭州", "南京") for repeat in range(2)]
    cases += [{"id": f"poi-{i}-{repeat}", "tool": "maps_search", "args": {"keywords": word, "city": city}}
              for i, (word, city) in enumerate((("杭州东站", "杭州"), ("无锡站", "无锡"), ("南京南站", "南京"))) for repeat in range(2)]
    manifest = begin(args.output, "real_read_tool_connection_pool", cases,
                     baseline="new httpx client per GET", candidate="one shared client with timeout=8",
                     source="live Amap read APIs through unchanged LifeTools methods; no MCP startup included",
                     concurrency=1, retries=0)
    with TemporaryDirectory(prefix="lifeplus-speed-tools-") as temporary:
        async with httpx.AsyncClient(timeout=8) as client:
            a = LifeTools(str(Path(temporary) / "unused.sqlite3"), settings.amap_maps_api_key)
            b = PooledTools(str(Path(temporary) / "unused.sqlite3"), settings.amap_maps_api_key, client=client)

            async def call(case, arm):
                result = await (a if arm == "baseline" else b).call(case["tool"], case["args"])
                passed = valid_tool(case["tool"], result)
                return {"result": result, "grade": {"passed": passed, "errors": [] if passed else [result.get("error_code", "missing_evidence")]}}

            rows = await paired(args.output, cases, call, timeout=15)
    result = finish(args.output, manifest, rows, scope="real read tool adapter benchmark; not full Agent or MCP round-trip")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


async def storage(args):
    cases = [{"id": f"storage-{i}", "children": 4, "deltas_per_child": 250, "delta": "示例文本片段。"} for i in range(5)]
    manifest = begin(args.output, "controlled_stream_storage", cases,
                     baseline="persist every delta; existing parent forwarding", candidate="coalesce 32 supplied deltas per child",
                     scope="synthetic SQLite write load; no model, network, arrival cadence or frontend", threshold_chars=256)

    async def call(case, arm):
        with TemporaryDirectory(prefix="lifeplus-speed-storage-") as temporary:
            db = Database(str(Path(temporary) / "events.sqlite3"))
            await db.open()
            try:
                now = datetime.now(timezone.utc).isoformat()
                for task_id, parent in [("root", None)] + [(f"c{i}", "root") for i in range(case["children"])]:
                    await db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at,parent_task_id) VALUES(?,?,?,?,?,?,?,?,?)",
                                     (task_id, "s", "lab", "test", "running", "live", now, now, parent))
                started = monotonic()
                writes = 0
                for i in range(case["children"]):
                    child = f"c{i}"
                    await db.publish(child, "answer.started", {"stream_id": child})
                    size = 1 if arm == "baseline" else 32
                    for offset in range(0, case["deltas_per_child"], size):
                        count = min(size, case["deltas_per_child"] - offset)
                        await db.publish(child, "answer.delta", {"stream_id": child, "delta": case["delta"] * count})
                        writes += 1
                    await db.publish(child, "task.completed", {"answer": case["delta"] * case["deltas_per_child"]})
                await db.publish("root", "task.completed", {})
                persist_ms = (monotonic() - started) * 1000
                valid = True
                for child in [f"c{i}" for i in range(case["children"])]:
                    events = await db.fetchall("SELECT type,data_json FROM events WHERE task_id=? ORDER BY seq", (child,))
                    answer = "".join(json.loads(e["data_json"]).get("delta", "") for e in events if e["type"] == "answer.delta")
                    valid &= answer == case["delta"] * case["deltas_per_child"] and events[-1]["type"] == "task.completed"
                root_events = await db.fetchall("SELECT seq,type FROM events WHERE task_id='root' ORDER BY seq")
                valid &= [r["seq"] for r in root_events] == list(range(1, len(root_events) + 1)) and root_events[-1]["type"] == "task.completed"
                return {"persist_ms": round(persist_ms, 3), "delta_publishes": writes, "root_events": len(root_events),
                        "grade": {"passed": bool(valid), "errors": [] if valid else ["text_or_order_mismatch"]}}
            finally:
                await db.close()

    rows = await paired(args.output, cases, call, timeout=60)
    result = finish(args.output, manifest, rows, scope="controlled storage microbenchmark, not SSE delivery or first-token latency",
                    untested=["time-based flush", "crash before buffered flush", "disconnect replay timing", "concurrent multi-client delivery"])
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("pool", "storage"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run((pool if args.stage == "pool" else storage)(args))


if __name__ == "__main__":
    main()
