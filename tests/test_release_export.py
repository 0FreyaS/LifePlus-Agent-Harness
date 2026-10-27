import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("prepare_release", Path(__file__).resolve().parents[1] / "scripts/prepare_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


@pytest.mark.parametrize("name", [".env", ".env.production", "frontend/.env.local", "data/runtime.sqlite3", "evaluations/runs/private/report.json", "src/car_agent/__pycache__/app.pyc", "frontend/node_modules/x/index.js", "frontend/dist/app.js", "deploy/server.key"])
def test_private_and_generated_files_are_not_released(name):
    assert not release.allowed(Path(name))


@pytest.mark.parametrize("name", [".env.example", "src/car_agent/api/app.py", "frontend/src/App.tsx", "Dockerfile", "docs/assets/showcase-summary.json", "evaluations/data/lifeplus-v1.jsonl", "evaluations/data/completion-v1.jsonl"])
def test_release_keeps_source_and_public_evidence(name):
    assert release.allowed(Path(name))


def test_export_is_secret_checked_and_preserves_existing_outputs(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".env").write_text("MODEL_API_KEY=real-private-test-value\n")
    (root / "README.md").write_text("safe source")
    output = tmp_path / "release"
    assert release.export(root, output)["files"] == 1
    assert not (output / ".env").exists()
    with pytest.raises(ValueError, match="never overwritten"):
        release.export(root, output)
    (root / "README.md").write_text("leak: real-private-test-value")
    with pytest.raises(ValueError, match="credential"):
        release.export(root, tmp_path / "blocked")
