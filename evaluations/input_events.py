"""Run real event interpretation in an isolated DB, without executing tools."""
import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from types import SimpleNamespace

from car_agent.config import Settings
from car_agent.freshness import FreshnessPolicy, utcnow
from car_agent.input_events import resolve_input
from car_agent.model_roles import build_models
from car_agent.persistence import Database
from car_agent.runtime_limits import RuntimeLimits
from evaluations.input_event_cases import CASES, HOLDOUT_CASES, COMPLETION_CASES, make_waiting, grade


async def run(args):
    output = args.output
    output.mkdir(parents=True, exist_ok=False)
    settings = Settings()
    cases = HOLDOUT_CASES if args.holdout else CASES
    model, roles = build_models(settings)
    if model is None:
        raise RuntimeError("Real configured model required")
    manifest = {"started_at": utcnow().isoformat(), "model": getattr(roles.get("intent", model), "model_name", None),
                "cases": len(cases), "dataset": "holdout" if args.holdout else "development", "concurrency": 1, "timeout_seconds": settings.input_event_timeout_seconds,
                "scope": "real configured model and production resolver; no business tools, HTTP, or browser execution",
                "cases_sha256": hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                "source_sha256": {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in
                    ("src/car_agent/input_events.py", "src/car_agent/user_events.py", "evaluations/input_event_cases.py")}}
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (output / "cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2))
    db = Database(str(output / "audit.sqlite3"))
    await db.open()
    context = SimpleNamespace(db=db, freshness=FreshnessPolicy(), limits=RuntimeLimits.from_settings(settings),
                              model_for=lambda role: roles.get(role, model))
    app = SimpleNamespace(state=SimpleNamespace(context=context, db=db, input_event_timeout=settings.input_event_timeout_seconds))
    rows = []
    try:
        for case in cases:
            started = time.monotonic()
            row = {"id": case["id"], "query": case["query"], "expected": case["expected"]}
            try:
                result = await resolve_input(app, case["query"], make_waiting(case, utcnow()), ["杭州", "无锡", "南京"],
                    owner="event-eval", session_id=case["id"], mode="live", request_id=case["id"], focus=case["focus"])
                row.update(source=result.source, decision=result.decision.model_dump(), grade=grade(case, result.decision), audit_id=result.audit_id)
            except Exception as exc:
                row.update(source="error", error_type=type(exc).__name__, grade={"passed": False, "errors": [type(exc).__name__]})
            row["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            rows.append(row)
            with (output / "results.jsonl").open("a") as file:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        await db.close()
    summary = {**manifest, "passed": sum(r["grade"]["passed"] for r in rows), "tested": len(rows),
               "by_source": {source: {"tested": sum(r["source"] == source for r in rows),
                    "passed": sum(r["source"] == source and r["grade"]["passed"] for r in rows)} for source in sorted({r["source"] for r in rows})},
               "failures": [{"id": r["id"], "errors": r["grade"]["errors"]} for r in rows if not r["grade"]["passed"]]}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary["passed"] == len(cases)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--write-completion-dataset", type=Path)
    parser.add_argument("--holdout", action="store_true")
    args = parser.parse_args()
    if args.write_completion_dataset:
        args.write_completion_dataset.parent.mkdir(parents=True, exist_ok=True)
        with args.write_completion_dataset.open("x") as file:
            for case in COMPLETION_CASES:
                file.write(json.dumps(case, ensure_ascii=False) + "\n")
        return
    if args.output is None:
        parser.error("--output is required when evaluating")
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()
