from evaluations.calibration_report import meter, sanitize
from evaluations.run import Usage


def test_token_meter_deduplicates_spans_and_uses_provider_total():
    rows = [{"type": "model.attempt.started", "span_id": "one"},
            {"type": "model.attempt.completed", "span_id": "one", "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 50}},
            {"type": "model.attempt.completed", "span_id": "one", "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 50}}]
    assert meter(rows) == {"model_attempts": 1, "known_total_tokens": 50, "attempts_missing_usage": 0, "total_tokens": 50}


def test_unknown_attempt_does_not_disappear_from_total_usage():
    rows = [{"type": "model.attempt.failed", "span_id": "one", "usage": {}},
            {"type": "model.attempt.completed", "span_id": "two", "usage": {"input_tokens": 20, "output_tokens": 10}}]
    assert meter(rows) == {"model_attempts": 2, "known_total_tokens": 30, "attempts_missing_usage": 1, "total_tokens": None}


def test_export_removes_credentials_contact_and_large_map_fields():
    assert sanitize({"api_key": "secret", "result": {"phone": "123", "geometry": [1, 2], "distance_m": 5}}) == {"result": {"distance_m": 5}}


def test_failed_provider_callback_keeps_usage_unknown_instead_of_zero():
    usage = Usage()
    assert usage.value()["total_tokens"] == 0
    usage.on_llm_error(TimeoutError())
    assert usage.value()["total_tokens"] is None
    assert usage.value()["responses_missing_usage"] == 1
