"""Single-arm real-model regression of frozen text events, without Jev prefilter."""
import argparse
import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .adversarial_cases import ROOT
from .adversarial_eval import live, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--dataset", type=Path, default=ROOT / "测试回报/测试数据集/06-文本事件/文本事件36条.jsonl")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.run_live:
        print(json.dumps({"paid_calls": 0, "status": "not_started"}))
        return
    if args.output is None or args.output.exists():
        raise ValueError("Provide a new output directory")
    raw = args.dataset.read_bytes()
    cases = [{"id": item["id"], "query": item["query"], "group": item["group"], "layer": "event_live", "event": item,
              "coverage_limit": "real_llm_direct_event_and_grounding; no_business_tools"}
             for item in map(json.loads, raw.decode().splitlines())]
    if len(cases) != 36 or len({case["id"] for case in cases}) != 36:
        raise ValueError("Expected frozen 36 text events")
    args.output.mkdir(parents=True)
    (args.output / "cases.jsonl").write_bytes(raw)
    (args.output / "manifest.json").write_text(json.dumps({"started_at": datetime.now(timezone.utc).isoformat(),
        "model": "deepseek-flash", "thinking": "disabled", "policy": "llm_direct", "concurrency": 1,
        "dataset_sha256": hashlib.sha256(raw).hexdigest(), "repeats": 1,
        "sources": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (ROOT / "src/car_agent").rglob("*.py")}}, ensure_ascii=False, indent=2))
    rows = asyncio.run(live(args.output, cases))
    print(json.dumps(write(args.output, rows, cases), ensure_ascii=False))


if __name__ == "__main__":
    main()
