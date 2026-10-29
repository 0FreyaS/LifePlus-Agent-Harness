"""Original zero-model heuristic vs real Jev cardinality, with explicit overhead."""
import argparse
import asyncio
import hashlib
import json
import random
from pathlib import Path
from time import monotonic

from .jev_ab import attempts_from_events, summarize, write_summary
from .jev_gate_cases import CASES


async def run(args):
    from car_agent.config import Settings
    from car_agent.freshness import utcnow
    from car_agent.persistence import Database
    from car_agent.planning_gate import build_planning_gate
    from car_agent.runtime_limits import RuntimeLimits, execution_scope
    from car_agent.task_planning import may_have_multiple_tasks

    settings = Settings()
    gate = build_planning_gate(settings.model_copy(update={"planning_gate_provider": "jev_front"}))
    args.output.mkdir(parents=True, exist_ok=False)
    manifest = {"scope": "cardinality_only_not_global_plan_generation", "started_at": utcnow().isoformat(),
        "cases": CASES, "seed": args.seed, "model": settings.jev_model_name,
        "timeout": settings.jev_timeout_seconds, "min_confidence": settings.jev_min_confidence,
        "min_probability": settings.jev_min_probability,
        "dataset_sha256": hashlib.sha256(json.dumps(CASES, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
        "note": "Baseline is a zero-model regex. Domain-only accuracy measured separately. No automatic rollout."}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    db, rows = Database(str(args.output / "audit.sqlite3")), []
    await db.open()
    try:
        cases = list(CASES)
        random.Random(args.seed).shuffle(cases)
        for i, case in enumerate(cases):
            for arm in (("baseline", "candidate") if i % 2 == 0 else ("candidate", "baseline")):
                identifier = case["id"] + "-" + arm
                now = utcnow().isoformat()
                await db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (identifier, identifier, "gate-eval", case["query"], "running", "live", now, now))
                started = monotonic()
                value, domain = None, None
                if arm == "candidate":
                    async with execution_scope(db, identifier, RuntimeLimits.from_settings(settings), "evaluation"):
                        value, domain = await gate.classify(case["query"], case["context"])
                effective = value or ("multi" if may_have_multiple_tasks(case["query"]) else "single")
                events = [{"type": e["type"], **json.loads(e["data_json"])} for e in await db.fetchall(
                    "SELECT type,data_json FROM events WHERE task_id=? ORDER BY seq", (identifier,))]
                row = {**case, "arm": arm, "actual_label": effective, "passed": effective == case["expected_label"],
                    "elapsed_ms": round((monotonic() - started) * 1000), "fallback": arm == "candidate" and value is None,
                    "raw_cardinality": value, "domain": domain, "attempts": attempts_from_events(events),
                    "prechecks": [e for e in events if e["type"] == "planning.precheck.completed"]}
                rows.append(row)
                with (args.output / "results.jsonl").open("a") as output:
                    output.write(json.dumps(row, ensure_ascii=False) + "\n")
                write_summary(args.output, {**summarize(rows), "scope": manifest["scope"]})
                print(json.dumps({k: row[k] for k in ("id", "arm", "actual_label", "passed", "elapsed_ms")}), flush=True)
    finally:
        await gate.close()
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=20261029)
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
