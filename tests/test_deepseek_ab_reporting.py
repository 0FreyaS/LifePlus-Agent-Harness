import json

import pytest

from evaluations.deepseek_jev_ab import archive, freeze
from evaluations.deepseek_model_ab import cases, metrics


def row(identifier, arm, known=True):
    return {"id": identifier, "arm": arm, "group": "test", "passed": True, "elapsed_ms": 10,
        "attempts": [{"stage": "response", "elapsed_ms": 5, "usage_known": known,
            "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6} if known else {}}]}


def test_unknown_usage_stays_in_full_latency_but_not_known_paired_subset():
    rows = [row("known", arm) for arm in ("baseline", "candidate")]
    rows += [row("unknown", "baseline", False), row("unknown", "candidate")]
    result = metrics(rows)
    assert result["all"]["arms"]["baseline"]["tested"] == 2
    assert result["all"]["arms"]["baseline"]["total_tokens"] is None
    assert result["complete_usage_pairs"]["ids"] == ["known"]
    assert result["complete_usage_pairs"]["summary"]["arms"]["candidate"]["total_tokens"] == 6


def test_model_swap_has_four_query_families_not_eight_independent_queries():
    values = cases()
    assert len(values) == 8
    assert len({value["query"] for value in values}) == 4
    assert len({value["id"] for value in values}) == 8


def test_frozen_evidence_refuses_overwrite(tmp_path):
    path = tmp_path / "cases.jsonl"
    freeze(path, [{"id": "original"}])
    freeze(path, [{"id": "original"}])
    with pytest.raises(ValueError, match="Frozen evidence changed"):
        freeze(path, [{"id": "changed"}])
    assert json.loads(path.read_text())["id"] == "original"


def test_archive_refuses_incomplete_pairs(tmp_path):
    source = tmp_path / "private"
    source.mkdir()
    freeze(source / "results.jsonl", [row("one", "baseline")])
    with pytest.raises(ValueError, match="Incomplete paired evidence"):
        archive(source, tmp_path / "public", [{"id": "one"}])
