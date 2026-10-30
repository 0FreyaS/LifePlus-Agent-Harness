"""Run real-model components and separately measured controlled contracts."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from types import SimpleNamespace

from .adversarial_cases import DATA, ROOT, prepare
from .current_measurements import aggregate
from .input_event_cases import grade as event_grade, make_waiting
from .jev_ab import attempts_from_events
from .planner_cases import grade_goals
from .deepseek_baseline import ForcePlanner, PLANNER_NOW


def summarize(rows, cases):
    return {"submitted": len(rows), "expected": len(cases),
        "complete": len(rows) == len(cases) and {row["id"] for row in rows} == {case["id"] for case in cases},
        "by_layer": {layer: aggregate([row for row in rows if row["layer"] == layer])
                     for layer in sorted({case["layer"] for case in cases})},
        "failures": [{key: row[key] for key in ("id", "layer", "errors", "error_type") if key in row}
                     for row in rows if not row["passed"]],
        "scope": "different_layers_not_an_end_to_end_completion_rate"}


def write(output, rows, cases):
    value = summarize(rows, cases)
    (output / "summary.json").write_text(json.dumps(value, ensure_ascii=False, indent=2))
    return value


async def live(output, cases):
    from car_agent.config import get_settings
    from car_agent.domain_routing import build_domain_router
    from car_agent.freshness import FreshnessPolicy, utcnow
    from car_agent.harness import resolve_domain_intent
    from car_agent.input_events import resolve_input
    from car_agent.model_roles import build_models, select_model
    from car_agent.persistence import Database
    from car_agent.runtime_limits import RuntimeLimits, execution_scope
    from car_agent.task_planning import plan_tasks

    settings = get_settings()
    if settings.model_name != "deepseek-flash" or settings.model_thinking != "disabled":
        raise ValueError("Keep approved DeepSeek with thinking disabled")
    llm, roles = build_models(settings)
    if llm is None:
        raise ValueError("Real model credentials required")
    router = build_domain_router(settings)
    db = Database(str(output / "audit.sqlite3"))
    await db.open()
    runtime = SimpleNamespace(context=SimpleNamespace(llm=llm, model_roles=roles, domain_router=router))
    context = SimpleNamespace(db=db, freshness=FreshnessPolicy(), limits=RuntimeLimits.from_settings(settings),
        model_for=lambda role: roles.get(role, llm), input_event_router=None, input_event_policy="llm_direct")
    app = SimpleNamespace(state=SimpleNamespace(context=context, db=db, input_event_timeout=settings.input_event_timeout_seconds))
    rows = []
    try:
        for case in cases:
            if case["layer"] == "controlled_contract":
                continue
            cid, started = case["id"], monotonic()
            row = {"id": cid, "query": case["query"], "layer": case["layer"], "group": case["group"],
                   "passed": False, "attempts": [], "coverage_limit": case["coverage_limit"]}
            await db.execute("INSERT INTO tasks(id,session_id,user_id,query,status,mode,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (cid, cid, "robustness", case["query"], "running", "live", utcnow().isoformat(), utcnow().isoformat()))
            audit_id = None
            try:
                async with asyncio.timeout(75):
                    if case["layer"] == "event_live":
                        item = case["event"]
                        waiting = make_waiting(item, utcnow())
                        if item.get("expired"):
                            for pending_task in waiting:
                                interrupt = json.loads(pending_task["interrupt_json"])
                                interrupt["expires_at"] = "2000-01-01T00:00:00+00:00"
                                pending_task["interrupt_json"] = json.dumps(interrupt, ensure_ascii=False)
                        result = await resolve_input(app, item["query"], waiting, ["无锡", "南京", "杭州"],
                            owner="robustness", session_id=cid, mode="live", request_id=cid, focus=item["focus"])
                        audit_id = result.audit_id
                        grade = event_grade(item, result.decision)
                        row.update(prediction=result.decision.model_dump(), passed=grade["passed"], errors=grade["errors"])
                    else:
                        async with execution_scope(db, cid, context.limits, "planning" if case["layer"] == "planner_live" else "evaluation"):
                            if case["layer"] == "planner_live":
                                item = case["plan"]
                                result = await plan_tasks(item["query"], "live", select_model(runtime.context, "planner"),
                                    item.get("memory_brief", ""), now=PLANNER_NOW, gate=ForcePlanner(), variant=settings.planner_variant)
                                grades = [grade_goals({**item, "tasks": tasks}, result)
                                          for tasks in [item["tasks"], *item.get("alternatives", [])]]
                                best = min(grades, key=lambda g: len(g["errors"]))
                                row.update(prediction=result.model_dump() if result else None, passed=best["passed"], errors=best["errors"])
                            else:
                                result = await resolve_domain_intent({"original_query": case["query"], "domain": case["domain"], "mode": "live",
                                    "memory_brief": case["memory_brief"]}, {}, runtime)
                                errors = ["slot:" + key for key, value in case["expected_slots"].items() if result.get("slots", {}).get(key) != value]
                                if "无锡" in json.dumps(result.get("slots", {}), ensure_ascii=False):
                                    errors.append("old_location_leakage")
                                row.update(prediction=result, errors=errors, passed=not errors)
            except Exception as exc:
                row.update(error_type=type(exc).__name__, errors=[type(exc).__name__])
            if case["layer"] == "event_live":
                if audit_id is None:
                    audit = await db.fetchone("SELECT id FROM input_event_decisions WHERE request_id=?", (cid,))
                    audit_id = audit["id"] if audit else None
                events = await db.fetchall("SELECT type,data_json FROM input_event_spans WHERE input_event_id=? ORDER BY seq", (audit_id,))
            else:
                events = await db.fetchall("SELECT type,data_json FROM events WHERE task_id=? ORDER BY seq", (cid,))
            events = [{"type": event["type"], **json.loads(event["data_json"])} for event in events]
            row["attempts"] = attempts_from_events(events)
            row["elapsed_ms"] = round((monotonic() - started) * 1000)
            rows.append(row)
            with (output / "results.jsonl").open("a") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            write(output, rows, cases)
            print(json.dumps({key: row[key] for key in ("id", "layer", "passed", "errors", "elapsed_ms")}, ensure_ascii=False), flush=True)
    finally:
        if router:
            await router.close()
        await db.close()
    return rows


def controlled(output, cases):
    extra = {
        "rag_lifecycle": ["evaluations/adversarial_contract_checks.py::test_rag_reopen_removes_deleted_or_shortened_chunks"],
        "sse_restart": ["tests/test_multi_task_api.py::test_sse_persists_actual_deltas_and_reconnect_honors_last_event_id",
                        "tests/test_multi_task_api.py::test_sse_replays_more_than_one_database_page_and_uses_highest_cursor",
                        "tests/test_multi_task_api.py::test_waiting_group_restores_independent_interrupt_after_restart"],
    }
    selectors = sorted({check for case in cases for check in case["checks"]} | {check for checks in extra.values() for check in checks})
    with tempfile.TemporaryDirectory(prefix="lifeplus-robustness-") as temp:
        subprocess.run(["rsync", "-a", "--include=.env.example", "--exclude=.env*", "--exclude=.git", "--exclude=.venv",
                        "--exclude=node_modules", "--exclude=__pycache__", "--exclude=.pytest_cache", "--exclude=/data",
                        "--exclude=/evaluations/runs", "--exclude=/frontend/dist", str(ROOT) + "/", temp + "/"], check=True)
        env = {key: value for key, value in os.environ.items() if not any(x in key.upper() for x in ("API_KEY", "PASSWORD", "SECRET"))}
        env["PYTHONPATH"] = str(Path(temp) / "src") + os.pathsep + temp
        env["ANONYMIZED_TELEMETRY"] = "False"
        args = [sys.executable, "-m", "pytest", "-q", *selectors, "--junitxml=" + str((output / "contracts.xml").resolve())]
        result = subprocess.run(args, cwd=temp, env=env, text=True, capture_output=True)
        (output / "contracts-output.txt").write_text(result.stdout + result.stderr)
        if not (output / "contracts.xml").exists():
            raise RuntimeError("Contract collection failed before JUnit")
    tests = ET.parse(output / "contracts.xml").findall(".//testcase")
    supplemental = {}
    for group, checks in extra.items():
        selected = [test for test in tests if any(
            test.get("classname") == check.split("::")[0].removesuffix(".py").replace("/", ".")
            and test.get("name", "").split("[", 1)[0] == check.split("::")[1] for check in checks)]
        supplemental[group] = [{"id": test.get("name"), "passed": not any(test.find(k) is not None for k in ("failure", "error", "skipped")),
                                "blocked": test.find("skipped") is not None,
                                "elapsed_ms": round(float(test.get("time", 0)) * 1000)} for test in selected]
    (output / "supplemental-contracts.json").write_text(json.dumps(supplemental, ensure_ascii=False, indent=2))
    rows = []
    for case in cases:
        if case["layer"] != "controlled_contract":
            continue
        selected = [test for test in tests if any(
            test.get("classname") == check.split("::")[0].removesuffix(".py").replace("/", ".")
            and (test.get("name") == check.split("::")[1] or test.get("name", "").startswith(check.split("::")[1] + "["))
            for check in case["checks"])]
        failed = [test for test in selected if test.find("failure") is not None or test.find("error") is not None or test.find("skipped") is not None]
        observed_selectors = {check for check in case["checks"] if any(
            test.get("classname") == check.split("::")[0].removesuffix(".py").replace("/", ".")
            and test.get("name", "").split("[", 1)[0] == check.split("::")[1] for test in selected)}
        row = {"id": case["id"], "layer": case["layer"], "group": case["group"], "query": case["query"],
            "passed": bool(selected) and not failed and len(observed_selectors) == len(case["checks"]),
            "assertion_cases": len(selected), "elapsed_ms": round(sum(float(test.get("time", 0)) for test in selected) * 1000),
            "errors": [test.get("classname") + "::" + test.get("name") for test in failed],
            "attempts": [], "coverage_limit": case["coverage_limit"]}
        if len(observed_selectors) != len(case["checks"]):
            row["errors"].append("missing_contract_test")
        rows.append(row)
    return rows


def run(args):
    if args.output.exists():
        raise ValueError("Use a new run directory")
    prepare()
    cases = [json.loads(line) for line in (ROOT / DATA).read_text().splitlines()]
    args.output.mkdir(parents=True)
    raw = (ROOT / DATA).read_bytes()
    (args.output / "cases.jsonl").write_bytes(raw)
    manifest = {"started_at": datetime.now(timezone.utc).isoformat(), "thinking": "disabled", "model": "deepseek-flash",
        "dataset_sha256": hashlib.sha256(raw).hexdigest(), "counts_by_layer": dict(Counter(row["layer"] for row in cases)),
        "source_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in [*sorted((ROOT / "src/car_agent").rglob("*.py")), Path(__file__), ROOT / "evaluations/adversarial_cases.py", ROOT / "evaluations/adversarial_contract_checks.py"]},
        "scope": "single_arm_components_and_separate_controlled_fault_tests; no_prompt_or_business_changes; no_production_deployment"}
    if args.reuse_live:
        old = json.loads((args.reuse_live / "manifest.json").read_text())
        if old["dataset_sha256"] != manifest["dataset_sha256"] or any(
            old["source_sha256"].get(key) != value for key, value in manifest["source_sha256"].items() if key.startswith("src/")):
            raise ValueError("Cannot reuse observations after input or runtime source changes")
        manifest.update(live_observations_reused_from=args.reuse_live.name, paid_calls_in_this_run=0,
                        contract_rerun_reason="Fix audit fixtures: attempt column; actual domain authorize node; create task row for persisted retry spans. Missing host Chroma is blocked, not a product failure")
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    rows = ([row for row in map(json.loads, (args.reuse_live / "results.jsonl").read_text().splitlines()) if row["layer"] != "controlled_contract"]
            if args.reuse_live else asyncio.run(live(args.output, cases)))
    rows.extend(controlled(args.output, cases))
    (args.output / "results.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
    print(json.dumps(write(args.output, rows, cases), ensure_ascii=False), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--reuse-live", type=Path)
    args = parser.parse_args(argv)
    if not args.run_live and not args.reuse_live:
        print(json.dumps({"paid_calls": 0, "status": "not_started", "require": "--run-live --output <new-dir>"}))
        return
    if args.output is None:
        parser.error("--output required")
    run(args)


if __name__ == "__main__":
    main()
