"""Audit actual Chroma in an existing container using temporary directories only."""
import argparse
import hashlib
import inspect
import json
import subprocess
from pathlib import Path


def assess(source_text=None):
    import hashlib
    import tempfile
    from pathlib import Path
    from car_agent.rag import KnowledgeRetriever
    import car_agent.rag

    if source_text is not None:
        from types import ModuleType
        isolated = ModuleType("_lifeplus_rag_audit")
        exec(compile(source_text, "<current-rag-source>", "exec"), isolated.__dict__)
        KnowledgeRetriever = isolated.KnowledgeRetriever

    rows = []
    for operation, expected in (("delete", 0), ("shrink", 1)):
        with tempfile.TemporaryDirectory(prefix="lifeplus-rag-audit-") as temp:
            root = Path(temp)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            source = knowledge / "policy.md"
            source.write_text("# 当前规则\n新内容\n# 废弃条款\n旧条款特有暗号旧版优惠")
            first = KnowledgeRetriever(root / "index", knowledge)
            first.open()
            before = first.collection.count() if first.backend == "chroma" else len(first._documents)
            if operation == "delete":
                source.unlink()
            else:
                source.write_text("# 当前规则\n新内容")
            second = KnowledgeRetriever(root / "index", knowledge)
            second.open()
            after = second.collection.count() if second.backend == "chroma" else len(second._documents)
            stale = any("旧条款特有暗号" in item["text"] for item in second.search("旧条款特有暗号", limit=3))
            rows.append({"id": "rag-current-" + operation, "backend": second.backend, "before": before,
                         "expected": expected, "after": after, "stale_retrievable": stale,
                         "passed": second.backend == "chroma" and after == expected and not stale})
    raw = source_text.encode() if source_text is not None else Path(car_agent.rag.__file__).read_bytes()
    return {"source_sha256": hashlib.sha256(raw).hexdigest(), "cases": rows,
            "source_origin": "supplied_working_tree_isolated_module" if source_text is not None else "installed_container_module",
            "scope": "existing_container_code_temporary_storage_no_model_no_service_restart_no_production_index_changes"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-current", action="store_true", help="Execute current rag.py in an isolated module, without replacing container files")
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("Use a new evidence file")
    source = Path("src/car_agent/rag.py").read_text() if args.source_current else None
    script = inspect.getsource(assess) + "\nimport json\nprint(json.dumps(assess(" + repr(source) + "), ensure_ascii=False))\n"
    result = subprocess.run(["docker", "exec", "-i", args.container, "python", "-"], input=script,
                            text=True, capture_output=True, timeout=90, check=True)
    value = json.loads(result.stdout.splitlines()[-1])
    value.update(container=args.container, current_source_matches=value["source_sha256"] == hashlib.sha256(Path("src/car_agent/rag.py").read_bytes()).hexdigest())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(value, ensure_ascii=False))


if __name__ == "__main__":
    main()
