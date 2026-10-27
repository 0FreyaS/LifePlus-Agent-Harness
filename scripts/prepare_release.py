"""Export an allowlisted, secret-checked source tree; never publish or git-add files."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {"src", "frontend", "skills", "knowledge", "docs", "evaluations", "tests", "scripts", "deploy", ".github"}
FILES = {"README.md", "SECURITY.md", "CONTRIBUTING.md", "LICENSE", "Dockerfile", ".dockerignore", ".gitignore",
         ".env.example", ".python-version", "docker-compose.yml", "pyproject.toml", "uv.lock", "requirements-production.txt"}
EXCLUDED = {"node_modules", "dist", "__pycache__", ".pytest_cache", ".venv", ".git", "runs", ".DS_Store"}
SECRET = re.compile(r"\b(?:xai-[A-Za-z0-9_-]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16})\b")


def allowed(path: Path) -> bool:
    parts = path.parts
    if not parts or parts[0] == "data" or any(part in EXCLUDED for part in parts) or path.is_absolute():
        return False
    if parts[0] not in DIRECTORIES and str(path) not in FILES:
        return False
    name = path.name
    if name.startswith(".env") and name != ".env.example":
        return False
    if name.endswith((".pyc", ".pyo", ".tsbuildinfo", ".log", ".sqlite3", ".sqlite3-wal", ".sqlite3-shm", ".db", ".pem", ".key", ".zip")):
        return False
    return True


def private_values(root: Path) -> list[bytes]:
    # Compare against actual local credentials without putting their values into any report.
    from dotenv import dotenv_values

    return [value.encode() for key, value in dotenv_values(root / ".env").items()
            if value and len(value) >= 12 and re.search(r"KEY|PASSWORD|TOKEN|SECRET|DSN", key, re.I)]


def export(root: Path, destination: Path) -> dict:
    if destination.exists() or destination.with_suffix(".zip").exists():
        raise ValueError("Choose a new release path; existing artifacts are never overwritten")
    selected = []
    private = private_values(root)
    for source in sorted(root.rglob("*")):
        relative = source.relative_to(root)
        if source.is_symlink() or not source.is_file() or not allowed(relative):
            continue
        content = source.read_bytes()
        if any(value in content for value in private) or SECRET.search(content.decode("utf-8", errors="ignore")):
            raise ValueError(f"Potential credential in release file: {relative}; export aborted")
        selected.append((source, relative))
    destination.mkdir(parents=True)
    for source, relative in selected:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    manifest = {"files": [str(relative) for _, relative in selected], "count": len(selected),
                "excluded": ["real credentials", "runtime databases", "chat history", "raw evaluation runs", "build caches"],
                "published": False, "license": "Confirm repository ownership and licensing before publishing"}
    (destination / "release-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    archive = destination.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                output.write(path, Path(destination.name) / path.relative_to(destination))
    return {"directory": str(destination), "archive": str(archive), "files": len(selected), "published": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(export(ROOT, args.output.resolve()), ensure_ascii=False))


if __name__ == "__main__":
    main()
