"""Opt-in planner-only diagnostics; never executes tools or overwrites A/B evidence."""
import argparse
import asyncio
import json
from pathlib import Path

from pydantic import ValidationError


async def run(output):
    from car_agent.config import Settings
    from car_agent.model_roles import build_models
    from car_agent.task_planning import GoalPlan, planning_prompt

    if output.exists():
        raise ValueError("Diagnostic evidence already exists; choose a new output")
    settings = Settings()
    model, roles = build_models(settings)
    model = roles.get("planner", model)
    if model is None or settings.model_name != "deepseek-flash":
        raise ValueError("Configured DeepSeek required")
    dataset = Path("evaluations/jev/2026-10-29-deepseek/v1/datasets/domain-e2e.jsonl")
    selected = [json.loads(line) for line in dataset.read_text().splitlines()
        if json.loads(line)["id"] in {"multi_completion-04", "jev-four-goals"}]
    call = model.with_structured_output(GoalPlan, method="json_mode", include_raw=True)
    results = []
    for case in selected:
        value = await call.ainvoke([{"role": "system", "content": planning_prompt()},
            {"role": "user", "content": case["query"]}])
        raw = value["raw"]
        row = {"id": case["id"], "query": case["query"], "content": raw.content,
            "usage": raw.usage_metadata, "valid": value["parsing_error"] is None}
        try:
            payload = json.loads(raw.content)
            GoalPlan.model_validate(payload)
        except ValidationError as error:
            row["validation_errors"] = [{"field": list(item["loc"]), "type": item["type"], "message": item["msg"]}
                for item in error.errors(include_url=False, include_input=False)]
        except (json.JSONDecodeError, TypeError) as error:
            row["json_error"] = type(error).__name__
        results.append(row)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"model": settings.model_name, "thinking": settings.model_thinking,
        "scope": "planner_only_no_tools", "results": results,
        "limitations": "Separate new diagnostic calls with empty memory and current time, not the exact original attempts; historical A/B remains unchanged."},
        ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(output), "results": [{"id": row["id"], "valid": row["valid"],
        "validation_errors": row.get("validation_errors")} for row in results]}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-live", action="store_true")
    parser.add_argument("--output", type=Path,
        default=Path("evaluations/jev/2026-10-29-deepseek/v1/diagnostics/planning-20261029.json"))
    args = parser.parse_args()
    if args.run_live:
        asyncio.run(run(args.output))
    else:
        print("Prepared only; --run-live makes two paid planner calls without tool execution.")


if __name__ == "__main__":
    main()
