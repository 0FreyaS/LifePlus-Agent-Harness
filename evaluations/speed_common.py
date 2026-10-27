"""Isolated latency experiments. Nothing here changes application configuration."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import random
import statistics
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def protected_snapshot():
    paths = [ROOT / name for name in (".env", ".env.example", "pyproject.toml", "uv.lock", "docker-compose.yml", "Dockerfile")]
    for directory in ("src", "frontend", "skills", "knowledge", "deploy"):
        paths.extend(p for p in (ROOT / directory).rglob("*") if p.is_file()
                     and not {"node_modules", "dist", "__pycache__"}.intersection(p.parts)
                     and not p.name.endswith((".pyc", ".tsbuildinfo")))
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.exists()}


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def begin(path: Path, experiment: str, cases: list, **conditions):
    path.mkdir(parents=True, exist_ok=False)
    manifest = {"experiment": experiment, "started_at": datetime.now().astimezone().isoformat(),
                "cases": len(cases), "approval_required": True, "auto_enable": False,
                "conditions": conditions, "quality_rule": "no new paired failure; candidate >=95% marked assertions",
                "large_gain_rule": "P50 reduction >=30%, sample P95 no worse than baseline +5%",
                "snapshot": protected_snapshot()}
    write_json(path / "manifest.json", manifest)
    write_json(path / "cases.json", cases)
    return manifest


def p95(values):
    return sorted(values)[max(0, math.ceil(len(values) * .95) - 1)] if values else None


def summarize(rows, expected_pairs):
    arms = {}
    for arm in ("baseline", "candidate"):
        selected = [r for r in rows if r["arm"] == arm]
        times = [r["elapsed_ms"] for r in selected]
        attempts = [a for r in selected for a in r.get("attempts", [])]
        known = all(a.get("usage") is not None for a in attempts)
        tokens = {key: sum((a.get("usage") or {}).get(key, 0) for a in attempts)
                  for key in ("input_tokens", "output_tokens", "total_tokens")}
        tokens["reasoning_tokens"] = sum((a.get("usage") or {}).get("output_token_details", {}).get("reasoning", 0) for a in attempts)
        arms[arm] = {"requests": len(selected), "passed": sum(r["grade"]["passed"] for r in selected),
                     "exceptions": sum(bool(r.get("error_type")) or any(a.get("error") or a.get("error_type")
                                                                          for a in r.get("attempts", [])) for r in selected),
                     "p50_ms": statistics.median(times) if times else None, "p95_ms": p95(times),
                     "model_calls": len(attempts), "usage_known": known, "reported_tokens": tokens}
    pairs = {}
    for row in rows:
        pairs.setdefault(row["case_id"], {})[row["arm"]] = row
    complete = [pair for pair in pairs.values() if len(pair) == 2]
    regressions = [pair["candidate"]["case_id"] for pair in complete
                   if pair["baseline"]["grade"]["passed"] and not pair["candidate"]["grade"]["passed"]]
    changes = [(pair["baseline"]["elapsed_ms"] - pair["candidate"]["elapsed_ms"]) / pair["baseline"]["elapsed_ms"]
               for pair in complete if pair["baseline"]["elapsed_ms"] > 0]
    rng = random.Random(271028)
    boot = sorted(statistics.median(rng.choices(changes, k=len(changes))) for _ in range(3000)) if changes else []
    ci = [boot[int(len(boot) * .025)], boot[min(len(boot) - 1, int(len(boot) * .975))]] if boot else [None, None]
    a, b = arms["baseline"], arms["candidate"]
    reduction = 1 - b["p50_ms"] / a["p50_ms"] if a["p50_ms"] and b["p50_ms"] is not None else None
    checks = {"complete": len(complete) == expected_pairs,
              "no_exceptions": a["exceptions"] == b["exceptions"] == 0,
              "candidate_quality": b["requests"] > 0 and b["passed"] / b["requests"] >= .95,
              "no_paired_regression": not regressions and b["passed"] >= a["passed"],
              "large_median_gain": reduction is not None and reduction >= .3,
              "tail_not_worse": bool(a["p95_ms"]) and b["p95_ms"] <= a["p95_ms"] * 1.05,
              "paired_interval_positive": ci[0] is not None and ci[0] > 0}
    return {"arms": arms, "paired_regressions": regressions, "p50_reduction": reduction,
            "paired_median_change_ci95": ci, "checks": checks,
            "large_gain_observed": all(checks.values()), "auto_enable": False,
            "decision": "await_user_confirmation", "limits": ["small synthetic paired pilot, not production SLA",
            "P50 is median; P95 is nearest-rank; exceptions remain in denominators and elapsed times",
            "paired bootstrap is by case; provider cache and network load are not controlled",
            "token counts are provider reports, not money; no usage is invented when absent"]}


def finish(path: Path, manifest, rows, **extra):
    result = summarize(rows, manifest["cases"])
    result.update(extra)
    current = protected_snapshot()
    result["runtime_unchanged"] = current == manifest["snapshot"]
    result["changed_runtime_files"] = sorted(k for k in current.keys() | manifest["snapshot"].keys()
                                             if current.get(k) != manifest["snapshot"].get(k))
    write_json(path / "summary.json", result)
    if not result["runtime_unchanged"]:
        raise AssertionError("Protected runtime files changed; no experiment may enable itself")
    return result


async def paired(path, cases, callback, seed=271027, timeout=50):
    jobs = list(cases)
    random.Random(seed).shuffle(jobs)
    rows = []
    failures = 0
    for index, case in enumerate(jobs):
        order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
        for arm in order:
            started = __import__("time").monotonic()
            try:
                async with asyncio.timeout(timeout):
                    row = await callback(case, arm)
            except Exception as exc:
                row = {"error_type": type(exc).__name__, "grade": {"passed": False, "errors": [type(exc).__name__]}}
            row.update(case_id=case["id"], arm=arm, elapsed_ms=round((__import__("time").monotonic() - started) * 1000, 3))
            rows.append(row)
            with (path / "results.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(json.dumps({key: row[key] for key in ("case_id", "arm", "elapsed_ms", "grade")}, ensure_ascii=False), flush=True)
            failures = failures + 1 if row.get("error_type") in {"APIConnectionError", "APITimeoutError", "OpenAIConnectionError", "OpenAITimeoutError"} else 0
            if failures >= 3:
                return rows
    return rows
