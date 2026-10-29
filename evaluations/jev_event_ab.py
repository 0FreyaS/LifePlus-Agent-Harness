"""Paired real-provider event interpretation, without business tool execution."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from .input_event_cases import CASES, HOLDOUT_CASES, FRONT_HOLDOUT_CASES, grade, make_waiting
from .jev_ab import attempts_from_events, summarize, write_summary


async def run(args):
    from car_agent.config import Settings
    from car_agent.event_routing import build_event_router
    from car_agent.freshness import FreshnessPolicy, utcnow
    from car_agent.input_events import resolve_input, rule_decision
    from car_agent.model_roles import build_models
    from car_agent.persistence import Database
    from car_agent.runtime_limits import RuntimeLimits

    settings = Settings()
    model, roles = build_models(settings)
    if model is None:
        raise ValueError("Baseline model configuration required")
    candidate = build_event_router(settings.model_copy(update={"input_event_provider": args.strategy}))
    cases = [json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()] if args.dataset else (
        FRONT_HOLDOUT_CASES if args.front_holdout else HOLDOUT_CASES if args.holdout else CASES)
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {"scope": "local_full_event_interpretation_no_business_tools", "strategy": args.strategy,
        "baseline_policy": args.baseline_policy, "started_at": utcnow().isoformat(),
        "cases": cases, "split": "front_holdout" if args.front_holdout else "holdout" if args.holdout else "development", "repeats": args.repeats, "seed": args.seed,
        "baseline_model": getattr(roles.get("intent", model), "model_name", None), "candidate_model": settings.jev_model_name,
        "jev_timeout_seconds": settings.jev_timeout_seconds, "event_timeout_seconds": settings.input_event_timeout_seconds,
        "min_confidence": settings.jev_min_confidence, "min_probability": settings.jev_min_probability,
        "dataset_sha256": hashlib.sha256(json.dumps(cases, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "sources": {path: hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in (
            "src/car_agent/event_routing.py", "src/car_agent/event_resume_filter.py",
            "src/car_agent/input_events.py", "src/car_agent/adapters/jev.py")}}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    db = Database(str(args.output / "audit.sqlite3"))
    await db.open()
    rows = []
    try:
        jobs = [(repeat, case) for repeat in range(args.repeats) for case in cases]
        random.Random(args.seed).shuffle(jobs)
        for index, (repeat, case) in enumerate(jobs):
            for arm in (("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")):
                waiting = make_waiting(case, utcnow())
                if case.get("expired"):
                    for task in waiting:
                        pending = json.loads(task["interrupt_json"])
                        pending["expires_at"] = "2020-01-01T00:00:00+00:00"
                        task["interrupt_json"] = json.dumps(pending, ensure_ascii=False)
                cities = ["杭州", "无锡", "南京"]
                group = case.get("group") or ("rules" if rule_decision(case["query"], waiting, cities, case["focus"]) is not None else "requires_model")
                context = SimpleNamespace(db=db, freshness=FreshnessPolicy(), limits=RuntimeLimits.from_settings(settings),
                    model_for=lambda role: roles.get(role, model), input_event_router=candidate if arm == "candidate" else None,
                    input_event_policy=args.strategy if arm == "candidate" else args.baseline_policy)
                app = SimpleNamespace(state=SimpleNamespace(context=context, db=db, input_event_timeout=settings.input_event_timeout_seconds))
                row = {"id": case["id"], "repeat": repeat, "arm": arm, "group": group, "passed": False,
                       "expected_label": case["expected"]["route"]}
                started = monotonic()
                try:
                    result = await resolve_input(app, case["query"], waiting, cities, owner="jev-event-eval", session_id=case["id"],
                        request_id=f"{case['id']}-{repeat}-{arm}", mode="live", focus=case["focus"])
                    row.update(source=result.source, decision=result.decision.model_dump(),
                        actual_label=result.decision.route, grade=grade(case, result.decision), audit_id=result.audit_id)
                    row["passed"] = row["grade"]["passed"]
                except Exception as exc:
                    row["error_type"] = type(exc).__name__
                    decision = await db.fetchone("SELECT id FROM input_event_decisions WHERE request_id=?", (f"{case['id']}-{repeat}-{arm}",))
                    row["audit_id"] = decision["id"] if decision else None
                row["elapsed_ms"] = round((monotonic() - started) * 1000)
                events = [{"type": event["type"], **json.loads(event["data_json"])} for event in await db.fetchall(
                    "SELECT type,data_json FROM input_event_spans WHERE input_event_id=? ORDER BY seq", (row.get("audit_id"),))]
                row["attempts"] = attempts_from_events(events)
                row["fallback"] = any(e["type"] == "input.precheck.completed" and not e.get("accepted") for e in events)
                row["prechecks"] = [e for e in events if e["type"] == "input.precheck.completed"]
                if case.get("expected_filter"):
                    row["expected_filter"] = case["expected_filter"]
                    actual_filter = next((e.get("answer", {}).get("choice") for e in row["prechecks"] if e.get("answer")), None)
                    row["actual_filter"] = actual_filter
                    row["filter_label_correct"] = actual_filter == case["expected_filter"] if actual_filter else None
                    row["unsafe_shortcut"] = row.get("source") == "jev" and case["expected_filter"] != "resume_only"
                rows.append(row)
                with (args.output / "results.jsonl").open("a", encoding="utf-8") as file:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
                write_summary(args.output, {**summarize(rows), "scope": manifest["scope"]})
                print(json.dumps({key: row[key] for key in ("id", "arm", "group", "passed", "elapsed_ms")}), flush=True)
    finally:
        await candidate.close()
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--holdout", action="store_true")
    parser.add_argument("--front-holdout", action="store_true")
    parser.add_argument("--strategy", choices=("jev_cascade", "jev_gate", "jev_resume_filter"), default="jev_cascade")
    parser.add_argument("--baseline-policy", choices=("llm", "llm_direct"), default="llm")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--seed", type=int, default=20261029)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("Positive repeats required")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
