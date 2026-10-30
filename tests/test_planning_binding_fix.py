import copy
import json
from collections import Counter
from types import SimpleNamespace

import pytest
from langchain_core.exceptions import OutputParserException
from langchain_core.messages import AIMessage
from pydantic import ValidationError

from car_agent.structured_output import invoke_structured
from car_agent.task_planning import GoalPlan, TaskPlan, planning_prompt
from evaluations.planner_binding_cases import SEED_IDS, dataset, long_tail
from evaluations.planner_binding_eval import main as evaluation_main


def expected_shape(item):
    return {"tasks": [{"id": spec["label"], "domain": spec["domain"],
        "query": " ".join(spec["query_terms"]) or spec["action"],
        "depends_on": sorted({source for source, _ in spec["binds"].values()}),
        "bind_slots": {target: source + "." + field for target, (source, field) in spec["binds"].items()}}
        for spec in item["tasks"]]}


def test_model_schema_exposes_binding_keys_and_paths_without_tool_slot_schema():
    schema = GoalPlan.model_json_schema()["$defs"]["TaskGoal"]["properties"]["bind_slots"]
    keys = schema["propertyNames"]["enum"]
    assert "details" in keys and not set(keys) & {"result", "content", "route", "weather_result", "user_id"}
    assert "selected|route|result|slots" in schema["additionalProperties"]["pattern"]
    assert "forecast_date" not in planning_prompt()
    assert '"details":"t1.result"' in planning_prompt()


@pytest.mark.parametrize("item", dataset(), ids=lambda item: item["id"])
def test_all_37_frozen_gold_binding_shapes_satisfy_host_contract(item):
    # This verifies labels and the host schema, not real-model accuracy.
    raw = expected_shape(item)
    assert len(GoalPlan.model_validate(raw).tasks) == len(item["tasks"])
    assert len(TaskPlan.model_validate(raw).tasks) == len(item["tasks"])


@pytest.mark.parametrize("item", [item for item in dataset() if item["id"] in SEED_IDS], ids=lambda item: item["id"])
def test_original_seven_failures_cannot_hide_invalid_target_or_drop_binding(item):
    raw = expected_shape(item)
    bound = next(spec for spec in raw["tasks"] if spec["bind_slots"])
    path = bound["bind_slots"].pop("details")
    bound["bind_slots"]["route" if path.endswith(".route") else "result"] = path
    with pytest.raises(ValidationError) as error:
        GoalPlan.model_validate(raw)
    assert error.value.errors()[0]["type"] == "literal_error"
    assert "bind_slots" in error.value.errors()[0]["loc"]


@pytest.mark.parametrize("path", ["w.tool_result", "w.result.", "w.result..city", "w", "w.result[0]", "w.result;ignore"])
def test_bad_source_paths_remain_rejected(path):
    raw = expected_shape(next(item for item in dataset() if item["id"] == "dependency01"))
    raw["tasks"][1]["bind_slots"] = {"details": path}
    with pytest.raises(ValidationError):
        GoalPlan.model_validate(raw)


def test_receiver_domain_restriction_and_direct_dependency_are_enforced():
    raw = expected_shape(next(item for item in dataset() if item["id"] == "dependency01"))
    raw["tasks"][1]["bind_slots"] = {"city": "w.result.city"}
    with pytest.raises(ValidationError) as error:
        GoalPlan.model_validate(raw)
    assert error.value.errors()[0]["type"] == "binding_target_domain"
    assert error.value.errors()[0]["ctx"]["allowed"] == "details,remind_at,reminder_id,title"
    raw["tasks"][1]["bind_slots"] = {"details": "w.result"}
    raw["tasks"][1]["depends_on"] = []
    with pytest.raises(ValidationError) as error:
        GoalPlan.model_validate(raw)
    assert error.value.errors()[0]["type"] == "binding_source_dependency"


class ParserWrappedModel:
    def __init__(self, invalid, valid=None):
        self.invalid, self.valid, self.calls = invalid, valid, []

    def with_structured_output(self, schema, **kwargs):
        assert schema is GoalPlan and kwargs["include_raw"]
        return self

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if len(self.calls) > 1 and self.valid is not None:
            return self.valid
        try:
            GoalPlan.model_validate(self.invalid)
        except ValidationError as cause:
            error = OutputParserException("Do not copy a provider secret or hidden reasoning")
            error.__cause__ = cause
            return {"raw": AIMessage(content=json.dumps(self.invalid), usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
                    "parsed": None, "parsing_error": error}


@pytest.mark.asyncio
async def test_wrapped_parser_error_gives_precise_bounded_repair_and_trace():
    valid = expected_shape(next(item for item in dataset() if item["id"] == "dependency01"))
    invalid = copy.deepcopy(valid)
    invalid["tasks"][1]["bind_slots"] = {"content": "w.result"}
    model, events = ParserWrappedModel(invalid, valid), []
    original = [{"role": "user", "content": "查无锡天气并保存，不要创建提醒"}]
    result = await invoke_structured(model, GoalPlan, original, stage="plan",
        report=lambda kind, **data: events.append({"type": kind, **data}))
    assert result.tasks[1].bind_slots == {"details": "w.result"}
    assert len(model.calls) == 2 and model.calls[1][0] == original[0] and len(original) == 1
    repair = model.calls[1][-1]["content"]
    assert "tasks.1.bind_slots.content.[key]" in repair and "details" in repair
    assert "Do not copy" not in repair and "hidden reasoning" not in repair
    assert events[0]["diagnostics"][0]["type"] == "literal_error"
    repeated = ParserWrappedModel(invalid)
    with pytest.raises(OutputParserException):
        await invoke_structured(repeated, GoalPlan, original, stage="plan")
    assert len(repeated.calls) == 2


def test_dataset_separates_old_failures_new_long_tail_and_no_write_controls():
    data = dataset()
    assert Counter(item["scope"] for item in data) == {"failed_seed7": 7, "long_tail30": 30}
    assert len(long_tail()) == 30 and set(Counter(item["group"] for item in long_tail()).values()) == {6}
    assert sum(not any(spec["binds"] for spec in item["tasks"]) for item in data) == 3


def test_evaluation_cli_does_not_call_a_model_without_explicit_live_flag(monkeypatch, capsys):
    monkeypatch.setattr("evaluations.planner_binding_eval.run", lambda *_: pytest.fail("Unexpected live call"))
    evaluation_main([])
    assert json.loads(capsys.readouterr().out)["paid_calls"] == 0
