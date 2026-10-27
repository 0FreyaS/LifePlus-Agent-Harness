"""Summarize JUnit boundary tests without publishing raw traces or user data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.etree import ElementTree


def read_report(path: Path) -> dict:
    root = ElementTree.parse(path).getroot()
    cases = list(root.iter("testcase"))
    rows = [{"id": item.attrib.get("classname", "") + "::" + item.attrib["name"],
             "failed": item.find("failure") is not None or item.find("error") is not None,
             "skipped": item.find("skipped") is not None} for item in cases]
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    return {"tested": len(rows), "passed": sum(not row["failed"] and not row["skipped"] for row in rows),
            "failed": sum(row["failed"] for row in rows), "skipped": sum(row["skipped"] for row in rows),
            "seconds": round(sum(float(suite.attrib.get("time", 0)) for suite in suites), 3), "cases": rows}


def summarize(baseline: Path, before: Path, after: Path, regression: Path) -> dict:
    reports = {name: read_report(file) for name, file in {
        "baseline": baseline, "targeted_before": before, "targeted_after": after, "regression": regression}.items()}
    previous = reports["targeted_before"]["cases"]
    current = reports["targeted_after"]["cases"]
    same_cases = {item["id"] for item in previous} == {item["id"] for item in current}
    if not same_cases or reports["targeted_after"]["failed"] or reports["regression"]["failed"]:
        raise ValueError("Reports do not establish a passing comparable regression")
    return {"date": "2026-10-26", "scope": "controlled boundary fault injection; isolated SQLite; no production load claim",
            "same_targeted_case_ids": same_cases,
            "runs": {key: {field: value for field, value in report.items() if field != "cases"} for key, report in reports.items()},
            "reproduced_failures": [item["id"] for item in previous if item["failed"]],
            "changes": ["shared typed planning slots and binding targets", "duplicate dependency rejection",
                        "business-failure and boolean-envelope validation", "structured MCP response parsing"],
            "excluded": ["real model accuracy", "live provider availability", "distributed recovery", "QPS or production SLA"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("baseline", "before", "after", "regression", "output"):
        parser.add_argument("--" + name, required=True, type=Path)
    args = parser.parse_args()
    result = summarize(args.baseline, args.before, args.after, args.regression)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"scope": result["scope"], "runs": result["runs"], "same_cases": True}, ensure_ascii=False))


if __name__ == "__main__":
    main()
