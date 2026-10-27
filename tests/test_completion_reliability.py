import pytest
from types import SimpleNamespace
from langchain_core.exceptions import OutputParserException
from pydantic import BaseModel, ValidationError

from car_agent.structured_output import invoke_structured
from car_agent.task_planning import TaskPlan
from car_agent.task_planning import plan_tasks
from car_agent.harness import resolve_intent
from car_agent.user_events import selection_index, split_resume_message
from evaluations.runtime import RuntimeLab
from car_agent.execution_verification import verify_execution


@pytest.mark.parametrize("answer", ["无锡", "第二个", "明天上午9点"])
@pytest.mark.parametrize("separator", ["，同时", "同时", " 顺便", "；另外", "，"])
@pytest.mark.parametrize("command", ["搜索周杰伦的歌曲", "查杭州天气"])
def test_compound_answer_preserves_both_intents(answer, separator, command):
    assert split_resume_message(answer + separator + command) == (answer, command)


@pytest.mark.parametrize("text", ["第二个", "无锡校区", "明天上午9点", "同时音乐餐厅", "无锡，同时不要播放歌曲"])
def test_answer_without_explicit_positive_new_command_is_not_split(text):
    assert split_resume_message(text) == (text, "")


@pytest.mark.parametrize("value,expected", [({"text": "第二个"}, 2), ({"text": "我选第二个吧"}, 2),
    ({"text": "无锡校区"}, 2), ({"text": "1"}, 1), ({"index": 1}, 1),
    ({"index": True}, None), ({"index": 999}, None), ({"text": "第二人民医院"}, None)])
def test_candidate_selection_is_consistent_for_location_and_result(value, expected):
    candidates = [{"name": "南京校区"}, {"name": "无锡校区"}]
    assert selection_index(value, candidates) == expected


class Decision(BaseModel):
    domain: str


class ParseLLM:
    def __init__(self, failures=1, error=OutputParserException):
        self.calls = []
        self.failures = failures
        self.error = error

    def with_structured_output(self, schema, **_):
        parent = self

        class Model:
            async def ainvoke(self, messages):
                parent.calls.append(messages)
                if len(parent.calls) <= parent.failures:
                    raise parent.error("invalid output")
                return schema.model_validate({"domain": "weather"})
        return Model()


@pytest.mark.asyncio
async def test_schema_repair_is_bounded_and_preserves_original_request():
    llm = ParseLLM()
    events = []
    request = [{"role": "user", "content": "查无锡天气，不要创建提醒"}]
    result = await invoke_structured(llm, Decision, request, stage="intent", report=lambda kind, **data: events.append({"type": kind, **data}))
    assert result.domain == "weather"
    assert len(llm.calls) == 2
    assert llm.calls[1][0] == request[0]
    assert len(request) == 1
    assert events[0]["will_retry"] is True
    repeated = ParseLLM(failures=9)
    with pytest.raises(OutputParserException):
        await invoke_structured(repeated, Decision, request, stage="intent")
    assert len(repeated.calls) == 2


@pytest.mark.asyncio
async def test_provider_errors_are_not_treated_as_schema_errors():
    llm = ParseLLM(error=TimeoutError)
    with pytest.raises(TimeoutError):
        await invoke_structured(llm, Decision, [], stage="intent")
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_production_router_recovers_from_first_invalid_model_response():
    calls = []

    class LLM:
        def with_structured_output(self, schema, **_):
            class Model:
                async def ainvoke(self, messages):
                    calls.append(messages)
                    if len(calls) == 1:
                        raise OutputParserException("missing required domain")
                    return schema.model_validate({"domain": "weather", "slots": {"city": "无锡"}})
            return Model()

    result = await resolve_intent({"original_query": "查询无锡今天的天气", "mode": "live"}, {},
        SimpleNamespace(context=SimpleNamespace(llm=LLM())))
    assert len(calls) == 2
    assert result["domain"] == "weather" and result["slots"]["city"] == "无锡"
    assert result["action"] == "weather_lookup"


@pytest.mark.asyncio
async def test_production_planner_repairs_invalid_output_binding_before_running_tools():
    calls = []

    class LLM:
        def with_structured_output(self, schema, **_):
            class Model:
                async def ainvoke(self, messages):
                    calls.append(messages)
                    return schema.model_validate({"tasks": [
                        {"id": "w", "query": "查无锡天气", "domain": "weather", "action": "weather_lookup", "slots": {"city": "无锡"}},
                        {"id": "s", "query": "保存天气", "domain": "planning", "action": "save_itinerary", "depends_on": ["w"],
                         "bind_slots": {"details": "w.tool_result" if len(calls) == 1 else "w.result"}},
                    ]})
            return Model()

    result = await plan_tasks("查无锡天气，然后保存查询结果到行程", "live", LLM())
    assert len(calls) == 2
    assert result.tasks[1].bind_slots == {"details": "w.result"}


def test_plan_rejects_invalid_action_and_unbindable_evidence_path_before_execution():
    def plan(action="save_itinerary", path="weather.result"):
        return {"tasks": [
            {"id": "weather", "domain": "weather", "action": "weather_lookup", "query": "查天气"},
            {"id": "save", "domain": "planning", "action": action, "query": "保存天气", "depends_on": ["weather"], "bind_slots": {"details": path}}]}
    assert TaskPlan.model_validate(plan())
    with pytest.raises(ValidationError):
        TaskPlan.model_validate(plan(action="save_weather"))
    with pytest.raises(ValidationError):
        TaskPlan.model_validate(plan(path="weather.tool_result"))


@pytest.mark.asyncio
async def test_route_ambiguous_origin_can_be_selected_and_resumed_to_completion():
    async with RuntimeLab() as lab:
        async def dispatch(name, args, task_id):
            if name == "maps_route" and args["origin"] == "火车站":
                return {"error": "有两个同名起点", "requires_clarification": True, "field": "origin", "retryable": False,
                        "candidates": [{"name": "杭州东站", "location": "120.2,30.2"}, {"name": "杭州城站", "location": "120.3,30.3"}]}
            return lab.receipt(name, args)
        lab.handler = dispatch
        state = await lab.invoke("ambiguous-origin", {"domain": "navigation", "action": "navigation_search",
            "slots": {"origin": "火车站", "destination": "西湖", "route_requested": True, "travel_mode": "walking"}},
            query="从火车站导航到西湖")
        pending = lab.interruption(state)
        assert pending["kind"] == "selection" and pending["field"] == "origin"
        assert len(pending["candidates"]) == 2
        state = await lab.resume("ambiguous-origin", {"text": "第二个"})
        assert state.values["status"] == "completed"
        assert state.values["slots"]["origin"] == "120.3,30.3"
        assert lab.calls[-1]["tool"] == "maps_route"
        assert lab.calls[-1]["args"]["origin"] == "120.3,30.3"


@pytest.mark.parametrize("state,problem", [
    ({"domain": "weather", "mode": "live", "tool_result": {"simulated": True}}, "live_result_is_simulated"),
    ({"domain": "weather", "tool_result": {"ok": True}}, "weather_receipt_incomplete"),
    ({"domain": "navigation", "slots": {"route_requested": True}, "tool_result": {"items": [{"id": "poi"}]}}, "route_receipt_incomplete"),
    ({"domain": "music", "mode": "live", "tool_result": {"items": [{"id": "song"}]}}, "song_link_missing"),
    ({"domain": "planning", "action": "save_itinerary", "mode": "live", "operation_result": {"status": "saved"}}, "write_resource_id_missing"),
    ({"domain": "planning", "action": "list_reminders", "tool_result": {}}, "list_receipt_incomplete"),
])
def test_incomplete_evidence_cannot_be_treated_as_success(state, problem):
    result = verify_execution(state)
    assert result["enforce_failure"] and problem in result["problems"]


def test_empty_search_is_not_a_positive_match_but_is_a_valid_search_response():
    result = verify_execution({"domain": "music", "tool_result": {"items": []}})
    assert result["contract_valid"] and not result["positive_result"]
    assert not result["enforce_failure"]


@pytest.mark.asyncio
async def test_graph_refuses_completed_when_route_receipt_is_missing():
    async with RuntimeLab() as lab:
        async def dispatch(name, args, task_id):
            return {"ok": True} if name == "maps_route" else lab.receipt(name, args)
        lab.handler = dispatch
        state = await lab.invoke("missing-route-receipt", {"domain": "navigation", "action": "navigation_search",
            "slots": {"origin": "杭州东站", "destination": "西湖"}}, query="从杭州东站导航到西湖")
        assert state.values["status"] == "failed"
        assert "route_receipt_incomplete" in state.values["execution_verification"]["problems"]
