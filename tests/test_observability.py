from car_agent.observability import LangfuseTraceSink, sanitize


class FakeObservation:
    def __init__(self, name, trace_id, identifier):
        self.name = name
        self.trace_id = trace_id
        self.id = identifier
        self.updates = []
        self.ended = False

    def update(self, **values):
        self.updates.append(values)
        return self

    def end(self, **_values):
        self.ended = True
        return self


class FakeClient:
    def __init__(self):
        self.created = []
        self.shutdown_called = False

    @staticmethod
    def create_trace_id(*, seed=None):
        return (seed or "trace").encode().hex()[:32].ljust(32, "0")

    def start_observation(self, **values):
        trace_id = (values.get("trace_context") or {}).get("trace_id", "trace")
        observation = FakeObservation(values["name"], trace_id, f"span-{len(self.created)}")
        observation.values = values
        self.created.append(observation)
        return observation

    def flush(self):
        pass

    def shutdown(self):
        self.shutdown_called = True


def task():
    return {
        "task_id": "task-1", "session_id": "session-1", "user_id": "user@example.com",
        "request_id": "request-1", "query": "查天气", "mode": "live", "domain": "weather",
        "parent_task_id": None,
    }


def test_sanitize_masks_credentials_and_pii():
    value = sanitize({
        "api_key": "secret-value",
        "message": "联系 13800138000 或 me@example.com，令牌 sk-lf-abcdefghijklmnop",
    }, capture_content=True)
    assert value["api_key"] == "<redacted>"
    assert "13800138000" not in value["message"]
    assert "me@example.com" not in value["message"]
    assert "sk-lf-" not in value["message"]


def test_content_capture_can_be_disabled_without_losing_shape():
    value = sanitize({"query": "导航到东南大学", "count": 2}, capture_content=False)
    assert value == {"query": {"redacted": True, "characters": 7}, "count": 2}


def test_task_tool_lifecycle_is_nested_and_closed():
    client = FakeClient()
    sink = LangfuseTraceSink(client=client, public_key="pk-test", capture_content=True)
    sink.publish(task(), "task.queued", {"query": "查天气"})
    sink.publish(task(), "tool.started", {
        "job_id": "job-1", "tool_name": "maps_weather", "arguments": {"city": "无锡"},
    })
    sink.publish(task(), "tool.completed", {
        "job_id": "job-1", "tool_name": "maps_weather", "result": {"weather": "晴"},
    })
    sink.publish(task(), "model.attempt.started", {
        "span_id": "model-1", "stage": "response", "framework_traced": True,
    })
    sink.publish(task(), "task.completed", {"answer": "无锡今天晴。"})

    names = [item.name for item in client.created]
    assert names.count("process-lifeplus-task") == 1
    assert names.count("tool.maps_weather") == 1
    assert not any(name.startswith("llm.") for name in names)
    root = client.created[0]
    tool = next(item for item in client.created if item.name == "tool.maps_weather")
    assert tool.values["trace_context"]["parent_span_id"] == root.id
    assert tool.ended is True
    assert root.ended is True
    assert "task-1" not in sink.roots


def test_sink_failure_does_not_escape_business_path():
    class BrokenClient(FakeClient):
        def start_observation(self, **_values):
            raise RuntimeError("offline")

    sink = LangfuseTraceSink(client=BrokenClient(), public_key="pk-test")
    sink.publish(task(), "task.queued", {})
    assert sink.last_error == "RuntimeError"
