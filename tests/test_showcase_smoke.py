"""Offline fixtures are synthetic test evidence, never acceptance reports."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from evaluations import showcase_smoke as smoke


@pytest.fixture(autouse=True)
def forbid_real_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("showcase unit tests must not open network connections")

    monkeypatch.setattr("socket.socket.connect", forbidden)
    monkeypatch.setattr("socket.socket.connect_ex", forbidden)


@pytest.fixture
def evidence():
    tasks = []
    for role in smoke.ROLES:
        spec = {"id": f"plan-{role}", "domain": smoke.DOMAINS[role],
                "action": {"place": "navigation_search", "weather": "weather_lookup",
                           "music": "music_search", "route": "navigation_search"}[role],
                "depends_on": ["plan-place"] if role == "route" else [], "slots": {}}
        if role == "route":
            spec.update(bind_slots={"origin": "plan-place.selected.location"},
                        prepared_decision={"slots": {"origin": "120.21,30.29"}})
        tasks.append({"id": f"secret-{role}", "thread_id": f"thread-{role}",
                      "domain": smoke.DOMAINS[role], "status": "completed", "mode": "live",
                      "plan_json": json.dumps(spec), "query": "PRIVATE_QUERY", "answer": "PRIVATE_ANSWER"})
    events = []

    def event(event_type, role=None, **fields):
        seq = len(events) + 1
        stamp = datetime(2026, 10, 26, tzinfo=timezone.utc) + timedelta(seconds=seq)
        value = {"seq": seq, "type": event_type, "created_at": stamp.isoformat(), **fields}
        if role:
            value["child_task_id"] = f"secret-{role}"
        events.append(value)
        return value

    def call(role, name, args, result):
        job = f"job-{role}-{len(events)}"
        event("tool.started", role, tool_name=name, job_id=job, attempt=1, arguments=args)
        return event("tool.completed", role, tool_name=name, job_id=job, attempt=1, result=result)

    event("model.attempt.started", stage="plan", span_id="private-model-span")
    event("model.attempt.completed", stage="plan", span_id="private-model-span")
    for role in smoke.ROLES[:3]:
        event("subtask.task.started", role)
    poi = {"name": "杭州东站", "location": "120.21,30.29", "id": "private-poi", "address": "PRIVATE_ADDRESS"}
    place = call("place", "maps_search", {"keywords": "杭州东站", "city": "杭州"},
                 {"items": [poi, {"name": "杭州东站-西广场", "location": "120.2,30.2"}], "source": "高德地图"})
    event("subtask.interrupt.required", "place", kind="selection", id="private-interrupt", candidates=place["result"]["items"])
    event("user.resumed", "place", action="answer", has_new_task=False)
    event("subtask.task.started", "place")
    event("node.completed", "place", node="navigation.select", status="selected", selection=1)
    event("subtask.task.completed", "place")
    event("subtask.task.started", "route")
    call("weather", "maps_weather", {"city": "无锡", "forecast_date": "2026-10-26"},
         {"source": "高德天气", "city": "无锡市", "date": "2026-10-26", "day_weather": "晴"})
    event("subtask.task.completed", "weather")
    call("music", "search_music", {"keyword": "周杰伦"},
         {"source": "QQ音乐", "items": [{"name": "晴天", "singer": "周杰伦"}]})
    event("node.completed", "music", node="music.select", status="automatic")
    event("subtask.task.completed", "music")
    route = call("route", "maps_route", {"origin": "120.210000,30.290000", "destination": "杭州西湖", "travel_mode": "walking"},
                 {"source": "高德地图", "origin": "120.21,30.29", "destination": "120.1,30.2",
                  "travel_mode": "walking", "distance_m": 15000, "duration_s": 12000})
    event("subtask.task.completed", "route")
    return {"outcome": "completed", "today": "2026-10-26", "elapsed_seconds": 32.12345,
            "task_id": "PRIVATE_TASK", "session_id": "PRIVATE_SESSION",
            "task": {"status": "completed", "mode": "live", "children": tasks},
            "events": events, "health": {"model_configured": True, "maps_configured": True, "tools_backend": "real_mcp"},
            "candidate_selections": [{"task_id": "secret-place", "interrupt_id": "private-interrupt", "index": 1,
                                      "item": poi, "candidates": place["result"]["items"],
                                      "job_id": place["job_id"], "offered_seq": place["seq"]}],
            "route": {"task_id": "secret-route", "job_id": route["job_id"], **route["result"]}}


def test_four_task_evidence_passes_and_summary_is_pure(evidence):
    before = deepcopy(evidence)
    assert all(smoke.validate_report(evidence).values())
    summary = smoke.public_summary(evidence)
    assert summary["status"] == "passed"
    assert summary["selected_public_poi_name"] == "杭州东站"
    assert [b["depends_on"] for b in summary["branches"]] == [[], [], [], ["place"]]
    assert evidence == before
    assert summary == smoke.public_summary(evidence)


def test_public_allowlist_removes_identifiers_trace_and_personal_data(evidence):
    evidence.update(username="PRIVATE_USERNAME", password="PRIVATE_PASSWORD", query="PRIVATE_QUERY",
                    authorization="PRIVATE_AUTH", error="PRIVATE_ERROR", rawtrace="PRIVATE_TRACE")
    evidence["task"]["children"][0]["query"] = "张三 13800138000 private@example.com"
    evidence["candidate_selections"][0]["item"]["name"] = "杭州东站 张三 13800138000"
    rendered = json.dumps(smoke.public_summary(evidence), ensure_ascii=False)
    for value in ("PRIVATE", "secret-", "private-", "thread-", "张三", "13800138000", "private@example.com",
                  "task_id", "session_id", "created_at", "tool.started", "120.21", "plan-place"):
        assert value not in rendered
    assert set(smoke.public_summary(evidence)) == {
        "query", "status", "elapsed_seconds", "branches", "selected_public_poi_name", "checks"}


@pytest.mark.parametrize("change,check", [
    ("extra_task", "four_tasks_three_roots_one_dependency"),
    ("missing_music", "four_tasks_three_roots_one_dependency"),
    ("wrong_dependency", "four_tasks_three_roots_one_dependency"),
    ("duplicate_plan", "four_tasks_three_roots_one_dependency"),
    ("local_business", "four_tasks_three_roots_one_dependency"),
    ("failed_child", "all_completed"),
    ("missing_completion", "all_completed"),
    ("timeout", "capture_finished"),
    ("no_model", "live_model_and_mcp"),
    ("demo", "live_model_and_mcp"),
    ("no_tools", "place_search_receipt"),
    ("simulated", "place_search_receipt"),
    ("wrong_weather", "weather_today_receipt"),
    ("wrong_singer", "music_search_receipt"),
    ("second_choice", "first_candidates_only"),
    ("wrong_origin", "route_origin_bound_to_selected_place"),
    ("wrong_binding", "route_origin_bound_to_selected_place"),
    ("wrong_route_job", "route_origin_bound_to_selected_place"),
    ("driving", "walking_to_west_lake"),
    ("wrong_destination", "walking_to_west_lake"),
    ("write", "no_business_writes"),
    ("approval", "no_business_writes"),
    ("invalid_seq", "trace_order_valid"),
    ("missing_time", "trace_order_valid"),
    ("dependency_started_early", "dependency_completed_before_start"),
])
def test_invalid_evidence_cannot_pass(evidence, change, check):
    children, events = evidence["task"]["children"], evidence["events"]
    route_start = next(e for e in events if e["type"] == "tool.started" and e.get("tool_name") == "maps_route")
    if change == "extra_task":
        children.append(deepcopy(children[0]))
    elif change == "missing_music":
        children.pop(2)
    elif change in {"wrong_dependency", "wrong_binding", "duplicate_plan", "local_business"}:
        child = children[0] if change == "local_business" else children[3]
        spec = json.loads(child["plan_json"])
        if change == "wrong_dependency":
            spec["depends_on"] = ["plan-weather"]
        elif change == "wrong_binding":
            spec["bind_slots"]["origin"] = "plan-place.slots.origin"
        elif change == "duplicate_plan":
            spec["id"] = "plan-place"
        else:
            spec["domain"] = "local_life"
        child["plan_json"] = json.dumps(spec)
    elif change == "failed_child":
        children[2]["status"] = "failed"
    elif change == "missing_completion":
        evidence["events"] = [e for e in events if not (
            e.get("child_task_id") == "secret-music" and e["type"] == "subtask.task.completed")]
    elif change == "timeout":
        evidence["outcome"] = "timeout"
    elif change == "no_model":
        evidence["events"] = [e for e in events if not e["type"].startswith("model.")]
    elif change == "demo":
        children[0]["mode"] = "demo"
    elif change == "no_tools":
        evidence["events"] = [e for e in events if not e["type"].startswith("tool.")]
    elif change == "simulated":
        next(e for e in events if e["type"] == "tool.completed")["result"]["simulated"] = True
    elif change == "wrong_weather":
        next(e for e in events if e.get("tool_name") == "maps_weather" and "result" in e)["result"]["date"] = "2026-10-27"
    elif change == "wrong_singer":
        next(e for e in events if e.get("tool_name") == "search_music" and "result" in e)["result"]["items"][0]["singer"] = "其他歌手"
    elif change == "second_choice":
        evidence["candidate_selections"][0]["index"] = 2
    elif change == "wrong_origin":
        route_start["arguments"]["origin"] = "120.5,30.5"
    elif change == "wrong_route_job":
        evidence["route"]["job_id"] = "different-job"
    elif change == "driving":
        route_start["arguments"]["travel_mode"] = "driving"
    elif change == "wrong_destination":
        route_start["arguments"]["destination"] = "杭州灵隐寺"
    elif change == "write":
        route_start["tool_name"] = "save_itinerary"
    elif change == "approval":
        events.append({"type": "subtask.interrupt.required", "kind": "approval"})
    elif change == "invalid_seq":
        events[-1]["seq"] = events[-2]["seq"]
    elif change == "missing_time":
        events[-1].pop("created_at")
    elif change == "dependency_started_early":
        begun = next(e for e in events if e["type"] == "subtask.task.started" and e.get("child_task_id") == "secret-route")
        begun.update(seq=1, created_at=events[0]["created_at"])
    assert smoke.validate_report(evidence)[check] is False
    assert smoke.public_summary(evidence)["status"] == "failed"


def test_overlap_requires_both_trace_time_and_sequence(evidence):
    # Keep interleaved seq, but put every branch in a disjoint real-time interval.
    for event in evidence["events"]:
        role = event.get("child_task_id", "").removeprefix("secret-")
        offset = {"place": 0, "weather": 100, "music": 200, "route": 300}.get(role, 0)
        stamp = datetime.fromisoformat(event["created_at"]) + timedelta(seconds=offset)
        event["created_at"] = stamp.isoformat()
    assert not smoke.validate_report(evidence)["independent_execution_overlap"]


def test_waiting_for_selection_is_not_running_overlap(evidence):
    events = evidence["events"]
    interrupt = next(e for e in events if e["type"] == "subtask.interrupt.required")
    resumed = next(e for e in events if e["type"] == "user.resumed")
    def stamp(seq):
        return (datetime(2026, 10, 26, tzinfo=timezone.utc) + timedelta(seconds=seq)).isoformat()
    def event(kind, role, seq):
        return {"type": kind, "child_task_id": f"secret-{role}", "seq": seq, "created_at": stamp(seq)}
    synthetic = [event("subtask.task.started", "place", 1),
                 {**interrupt, "seq": 2, "created_at": stamp(2)},
                 event("subtask.task.started", "weather", 3), event("subtask.task.completed", "weather", 4),
                 event("subtask.task.started", "music", 5), event("subtask.task.completed", "music", 6),
                 {**resumed, "seq": 7, "created_at": stamp(7)},
                 event("subtask.task.started", "place", 8), event("subtask.task.completed", "place", 9)]
    evidence["events"] = synthetic
    assert not smoke.validate_report(evidence)["independent_execution_overlap"]


def test_summary_exposes_actual_dependency_aliases_on_failure(evidence):
    child = evidence["task"]["children"][3]
    spec = json.loads(child["plan_json"])
    spec["depends_on"] = ["plan-weather", "PRIVATE_UNKNOWN_TASK"]
    child["plan_json"] = json.dumps(spec)
    summary = smoke.public_summary(evidence)
    assert summary["branches"][3]["depends_on"] == ["weather", "unknown"]
    assert summary["status"] == "failed"
    assert "PRIVATE_UNKNOWN_TASK" not in json.dumps(summary)


@pytest.mark.parametrize("field,value", [("events", [None]), ("events", {"raw": "PRIVATE"}),
                                        ("health", []), ("task", {"children": None}),
                                        ("task", {"children": [None]})])
def test_malformed_evidence_is_a_failed_summary(evidence, field, value):
    evidence[field] = value
    assert smoke.public_summary(evidence)["status"] == "failed"


def test_single_place_candidate_uses_graph_automatic_selection(evidence):
    receipt = next(e for e in evidence["events"] if e.get("tool_name") == "maps_search" and "result" in e)
    receipt["result"]["items"] = receipt["result"]["items"][:1]
    evidence["candidate_selections"] = []
    selected = next(e for e in evidence["events"] if e.get("node") == "navigation.select")
    selected.pop("selection")
    selected["status"] = "automatic"
    assert smoke.validate_report(evidence)["place_search_receipt"]


def test_seq_serialized_intervals_do_not_overlap_even_with_overlapping_timestamps(evidence):
    segments = []
    for i, role in enumerate(smoke.ROLES[:3]):
        for end, kind in enumerate(("subtask.task.started", "subtask.task.completed")):
            segments.append({"type": kind, "child_task_id": f"secret-{role}", "seq": i * 2 + end + 1,
                             "created_at": f"2026-10-26T00:00:0{end}+00:00"})
    evidence["events"] = segments
    assert not smoke.validate_report(evidence)["independent_execution_overlap"]


def test_selection_rejects_unoffered_empty_and_expired_candidates(evidence):
    child = evidence["task"]["children"][0]
    pending = deepcopy(next(e for e in evidence["events"] if e["type"] == "subtask.interrupt.required"))
    assert smoke.selection_evidence(evidence["events"], child, pending)["index"] == 1
    assert smoke.selection_evidence([], child, pending) is None
    for update in ({"candidates": []}, {"expired": True}, {"kind": "approval"},
                   {"candidates": [{"name": "invented"}]}):
        assert smoke.selection_evidence(evidence["events"], child, {**pending, **update}) is None


def test_selection_can_use_actual_route_clarification_candidates(evidence):
    child = evidence["task"]["children"][3]
    candidates = [{"name": "西湖", "location": "120.1,30.2"}]
    events = [
        {"seq": 1, "type": "tool.started", "child_task_id": child["id"], "job_id": "route-job",
         "tool_name": "maps_route", "attempt": 1, "created_at": "2026-10-26T00:00:01+00:00"},
        {"seq": 2, "type": "tool.clarification_required", "child_task_id": child["id"], "job_id": "route-job",
         "tool_name": "maps_route", "attempt": 1, "created_at": "2026-10-26T00:00:02+00:00",
         "result": {"requires_clarification": True, "error": "多个地点", "candidates": candidates}},
    ]
    pending = {"id": "destination-interrupt", "kind": "selection", "candidates": candidates}
    assert smoke.selection_evidence(events, child, pending)["item"] == candidates[0]
    events[-1]["attempt"] = 2
    assert smoke.selection_evidence(events, child, pending) is None


@pytest.mark.asyncio
async def test_capture_uses_live_natural_language_and_only_answers_first_candidate(evidence):
    requests, polls = [], 0
    auth = httpx.BasicAuth("PRIVATE_USERNAME", "PRIVATE_PASSWORD")
    child = deepcopy(evidence["task"]["children"][0])
    child["status"] = "waiting_user"
    child["interrupt"] = next(e for e in evidence["events"] if e["type"] == "subtask.interrupt.required")

    async def handler(request):
        nonlocal polls
        requests.append(request)
        assert request.headers["authorization"].startswith("Basic ")
        path = request.url.path
        if path == "/api/health":
            result = evidence["health"]
        elif path == "/api/sessions":
            result = {"id": "session"}
        elif path.endswith("/messages"):
            assert json.loads(request.content) == {"message": smoke.QUERY, "mode": "live",
                                                   "request_id": json.loads(request.content)["request_id"]}
            result = {"task_id": "root"}
        elif path == "/api/tasks/root":
            polls += 1
            result = {**evidence["task"], "status": "waiting_user", "children": [child]} if polls == 1 else evidence["task"]
        elif path.endswith("/trace"):
            result = evidence["events"]
        elif path.endswith("/resume"):
            assert json.loads(request.content) == {"interrupt_id": "private-interrupt", "action": "answer", "value": {"index": 1}}
            result = {"status": "queued"}
        elif path.endswith("/route"):
            result = evidence["route"]
        else:
            raise AssertionError(path)
        return httpx.Response(200, json=result)

    report = await smoke.capture("http://offline.invalid", transport=httpx.MockTransport(handler),
                                 auth=auth, timeout=1, poll_interval=0.001)
    assert report["outcome"] == "completed"
    assert report["candidate_selections"][0]["item"]["name"] == "杭州东站"
    assert len([r for r in requests if r.url.path.endswith("/resume")]) == 1
    assert "PRIVATE_USERNAME" not in json.dumps(report)
    assert "PRIVATE_PASSWORD" not in json.dumps(report)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["approval", "clarification", "empty", "unoffered", "timeout", "http_error",
                                 "no_live", "invalid_health", "invalid_task", "invalid_trace"])
async def test_capture_failure_is_bounded_and_does_not_authorize_writes(evidence, kind):
    writes = []

    async def handler(request):
        path = request.url.path
        if request.method == "POST":
            writes.append(path)
        if kind == "http_error":
            return httpx.Response(401, text="PRIVATE_USERNAME PRIVATE_PASSWORD")
        if path == "/api/health":
            if kind == "invalid_health":
                return httpx.Response(200, json=[])
            return httpx.Response(200, json={} if kind == "no_live" else evidence["health"])
        if path == "/api/sessions":
            return httpx.Response(200, json={"id": "session"})
        if path.endswith("/messages"):
            return httpx.Response(200, json={"task_id": "root"})
        if path.endswith("/cancel"):
            return httpx.Response(200, json={"status": "cancelled"})
        if path.endswith("/trace"):
            if kind == "invalid_trace":
                return httpx.Response(200, json=[None])
            return httpx.Response(200, json=evidence["events"])
        if kind == "invalid_task":
            return httpx.Response(200, json={"children": None})
        child = deepcopy(evidence["task"]["children"][0])
        pending = deepcopy(next(e for e in evidence["events"] if e["type"] == "subtask.interrupt.required"))
        if kind in {"approval", "clarification"}:
            pending["kind"] = kind
        if kind == "empty":
            pending["candidates"] = []
        if kind == "unoffered":
            pending["candidates"] = [{"name": "not from tool"}]
        child["interrupt"] = {} if kind == "timeout" else pending
        return httpx.Response(200, json={"status": "running", "children": [child]})

    report = await smoke.capture("http://offline.invalid", transport=httpx.MockTransport(handler),
                                 timeout=0.03, poll_interval=0.001)
    assert report["outcome"] != "completed"
    assert report["elapsed_seconds"] < 1
    assert smoke.public_summary(report)["status"] == "failed"
    assert not any(path.endswith("/resume") for path in writes)
    assert "PRIVATE_USERNAME" not in json.dumps(report)
    if kind == "timeout":
        assert report["outcome"] == "timeout"
        assert "/api/tasks/root/cancel" in writes


def test_cli_creates_private_evidence_and_public_summary_without_auth_leaks(tmp_path, monkeypatch, capsys, evidence):
    calls = []

    async def offline_capture(base_url, **kwargs):
        calls.append((base_url, kwargs))
        return evidence

    monkeypatch.setattr(smoke, "capture", offline_capture)
    monkeypatch.setenv("SHOWCASE_USERNAME", "PRIVATE_USERNAME")
    monkeypatch.setenv("SHOWCASE_PASSWORD", "PRIVATE_PASSWORD")
    output = tmp_path / "new-report"
    assert smoke.main(["--output-dir", str(output)]) == 0
    assert calls[0][0] == "http://127.0.0.1:8012"
    assert isinstance(calls[0][1]["auth"], httpx.BasicAuth)
    private = output / "private data" / "report.json"
    assert private.stat().st_mode & 0o777 == 0o600
    assert private.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(private.read_text()) == evidence
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "passed"
    rendered = capsys.readouterr().out + private.read_text() + (output / "summary.json").read_text()
    assert "PRIVATE_USERNAME" not in rendered and "PRIVATE_PASSWORD" not in rendered
    with pytest.raises(SystemExit):
        smoke.main(["--output-dir", str(output)])
    assert len(calls) == 1


@pytest.mark.parametrize("args", [["--timeout", "nan"], ["--timeout", "0"], ["--poll-interval", "-1"],
                                 ["--base-url", "http://user:password@localhost:8012"]])
def test_cli_invalid_options_do_not_start_capture(tmp_path, args):
    output = tmp_path / "new"
    with pytest.raises(SystemExit):
        smoke.main(["--output-dir", str(output), *args])
    assert not output.exists()


def test_cli_rejects_half_configured_auth_and_returns_failure(tmp_path, monkeypatch, evidence):
    monkeypatch.setenv("SHOWCASE_USERNAME", "private-user")
    monkeypatch.delenv("SHOWCASE_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        smoke.main(["--output-dir", str(tmp_path / "half-auth")])
    assert not (tmp_path / "half-auth").exists()
    monkeypatch.delenv("SHOWCASE_USERNAME")

    async def fail(*args, **kwargs):
        return {**evidence, "outcome": "timeout"}

    monkeypatch.setattr(smoke, "capture", fail)
    assert smoke.main(["--output-dir", str(tmp_path / "failure")]) == 1
    assert json.loads((tmp_path / "failure" / "summary.json").read_text())["status"] == "failed"
