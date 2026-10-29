"""Interleave full isolated completion runs with original and Jev configuration."""
import argparse
import asyncio
import hashlib
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from .jev_ab import compare_e2e, read_e2e_rows, summarize, write_summary


async def run(args):
    cases = [json.loads(line) for line in args.dataset.read_text().splitlines() if line.strip()]
    if args.ids:
        cases = [case for case in cases if case["id"] in args.ids.split(",")]
    args.output.mkdir(parents=True, exist_ok=False)
    random.Random(args.seed).shuffle(cases)
    manifest = {"scope": "real_model_graph_mcp_storage_paired_pilot", "started_at": datetime.now(timezone.utc).isoformat(),
        "cases": [case["id"] for case in cases], "candidate": args.candidate, "seed": args.seed,
        "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(), "case_budget_seconds": args.case_budget,
        "concurrency": 1, "order": "alternating AB/BA by shuffled case", "auto_enable": False,
        "limitations": "Each case/arm has a fresh process and isolated storage. No browser/TCP cost or real human waiting."}
    (args.output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    rows = []
    for index, case in enumerate(cases):
        directories = {arm: args.output / (case["id"] + "-" + arm) for arm in ("baseline", "candidate")}
        for arm in (("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")):
            domain_enabled = arm == "candidate" and args.candidate in {"domain", "both"}
            events_enabled = arm == "candidate" and args.candidate in {"events", "both", "front"}
            gate_provider = "jev_front" if args.candidate == "front" else "jev_cardinality" if args.candidate == "cardinality" else "rules"
            env = {**os.environ, "DOMAIN_ROUTE_PROVIDER": "jev_cascade" if domain_enabled else "llm",
                   "INPUT_EVENT_PROVIDER": ("jev_gate" if args.candidate == "front" else "jev_cascade") if events_enabled else "llm",
                   "PLANNING_GATE_PROVIDER": gate_provider if arm == "candidate" else "rules"}
            command = [sys.executable, "-m", "evaluations.completion", "--dataset", str(args.dataset),
                       "--output", str(directories[arm]), "--ids", case["id"], "--concurrency", "1",
                       "--case-budget", str(args.case_budget)]
            print(json.dumps({"case": case["id"], "arm": arm, "status": "started"}), flush=True)
            with (args.output / (case["id"] + "-" + arm + ".log")).open("w", encoding="utf-8") as log:
                process = await asyncio.create_subprocess_exec(*command, env=env, stdout=log, stderr=log)
                try:
                    async with asyncio.timeout(args.case_budget + 75):
                        await process.wait()
                except BaseException:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                    raise
            if not (directories[arm] / "results.jsonl").exists():
                raise RuntimeError("Completion runner did not produce evidence; see its saved log")
            actual = read_e2e_rows(directories[arm], arm)
            stages = {attempt.get("stage") for row in actual for attempt in row["attempts"]}
            if arm == "candidate" and gate_provider != "rules" and "planning_gate_precheck" not in stages:
                raise RuntimeError("Configured planning gate did not execute")
            if domain_enabled and "domain_route" in stages and not any(row["precheck_counts"]["domain.precheck.completed"] for row in actual):
                raise RuntimeError("Candidate domain router was configured but did not reach task execution")
            if events_enabled and "input_events" in stages and not any(row["precheck_counts"]["input.precheck.completed"] for row in actual):
                raise RuntimeError("Candidate event router was configured but did not reach event interpretation")
            rows.extend(actual)
            with (args.output / "results.jsonl").open("a", encoding="utf-8") as file:
                for row in actual:
                    file.write(json.dumps(row, ensure_ascii=False) + "\n")
            write_summary(args.output, {**summarize(rows), "scope": manifest["scope"], "planned_pairs": len(cases)})
            print(json.dumps({"case": case["id"], "arm": arm, "passed": actual[0]["passed"],
                              "elapsed_ms": actual[0]["elapsed_ms"], "failures": actual[0].get("failures", [])}), flush=True)
        compare_e2e([directories["baseline"], directories["candidate"]], args.output / (case["id"] + "-comparison"))
    report = {**summarize(rows), "scope": manifest["scope"], "planned_pairs": len(cases)}
    write_summary(args.output, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=Path("evaluations/data/jev-e2e-v1.jsonl"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--candidate", choices=("domain", "events", "both", "cardinality", "front"), default="both")
    parser.add_argument("--ids")
    parser.add_argument("--case-budget", type=float, default=120)
    parser.add_argument("--seed", type=int, default=20261029)
    args = parser.parse_args()
    if args.case_budget <= 0:
        parser.error("Positive budget required")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
