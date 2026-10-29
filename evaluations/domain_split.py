"""Compare two source snapshots through planning and domain parameter readiness."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace


async def run(args):
    sys.path.insert(0, str(args.source / "src"))
    from car_agent.config import Settings
    from car_agent.model_roles import build_models
    from car_agent.task_planning import plan_tasks, TaskPlan
    from car_agent import harness
    from evaluations.planner_cases import CASES, case, weather, music, grade
    from evaluations.planner_ab import MeteredModel

    frozen = datetime.fromisoformat("2026-10-27T10:00:00+08:00")

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen.replace(tzinfo=None)

    harness.datetime = Clock
    selected = [c for c in CASES if c["id"] in {
        "single01", "single02", "single03", "single04", "single05",
        "parallel01", "parallel03", "parallel04", "parallel05", "dependency01", "dependency05", "context02"}]
    for name, delimiter in [("space", " "), ("newline", "\n"), ("period", "。"), ("enumeration", "、")]:
        selected.append(case("delimiter-" + name, "delimiter", "查无锡明天天气" + delimiter + "搜索周杰伦的歌曲", [weather(), music()]))
    if args.ids:
        selected = [item for item in selected if item["id"] in args.ids.split(",")]
    config = Settings()
    base, _ = build_models(config)
    if base is None:
        raise RuntimeError("Real model configuration required")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"started_at": datetime.now().astimezone().isoformat(), "model": config.model_name,
                "cases": [item["id"] for item in selected], "reference_time": frozen.isoformat(),
                "dataset_sha256": hashlib.sha256(json.dumps(selected, sort_keys=True, ensure_ascii=False).encode()).hexdigest(),
                "source_sha256": {name: hashlib.sha256((args.source / "src/car_agent" / name).read_bytes()).hexdigest()
                                  for name in ("task_planning.py", "harness.py", "orchestration.py")},
                "scope": "Real model planning and parameter readiness only; no tool execution. Dependency layers sequential, independent domain calls concurrent.",
                "case_timeout_seconds": 120, "provider_timeout_seconds": 25, "provider_retries": 0}
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = []
    for item in selected:
        model = MeteredModel(base)
        runtime = SimpleNamespace(context=SimpleNamespace(llm=model, model_roles={}))
        started = time.monotonic()
        result = {"id": item["id"], "group": item["group"], "source": str(args.source), "model": config.model_name}
        try:
            async with asyncio.timeout(120):
                plan = await plan_tasks(item["query"], "live", model, item["memory_brief"], now=frozen)
                plan_ms = round((time.monotonic() - started) * 1000)
                specs = plan.model_dump()["tasks"] if plan else [{"id": "single", "query": item["query"]}]

                async def understand(spec):
                    state = {"mode": "live", "original_query": spec["query"], "memory_brief": item["memory_brief"],
                             "memory": {}, "conversation_references": [],
                             "prepared_decision": {key: value for key, value in spec.items() if key in {"domain", "action", "slots"}}}
                    state.update(await harness.resolve_intent(state, {}, runtime))
                    if hasattr(harness, "resolve_domain_intent"):
                        state.update(await harness.resolve_domain_intent(state, {}, runtime))
                    return {**spec, "domain": state["domain"], "action": state["action"], "slots": state["slots"]}

                pending, ready = list(specs), {}
                while pending:
                    layer = [spec for spec in pending if set(spec.get("depends_on", [])) <= ready.keys()]
                    if not layer:
                        raise ValueError("Invalid dependency graph")
                    for value in await asyncio.gather(*(understand(spec) for spec in layer)):
                        ready[value["id"]] = value
                    pending = [spec for spec in pending if spec["id"] not in ready]
                actual = TaskPlan.model_validate({"tasks": list(ready.values())})
                result.update(plan_ms=plan_ms, plan=actual.model_dump(), grade=grade(item, actual))
        except Exception as exc:
            result.update(error=type(exc).__name__, grade={"passed": False, "errors": [type(exc).__name__]})
        result.update(elapsed_ms=round((time.monotonic() - started) * 1000), attempts=model.attempts)
        rows.append(result)
        args.output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"id": item["id"], "passed": result["grade"]["passed"], "errors": result["grade"]["errors"],
                          "elapsed_ms": result["elapsed_ms"]}, ensure_ascii=False), flush=True)
        if args.pause:
            await asyncio.sleep(args.pause)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ids")
    parser.add_argument("--pause", type=float, default=0)
    asyncio.run(run(parser.parse_args()))
