"""Execute the handbook's exact tree example; verify local references and scope."""
import ast
from copy import deepcopy
from pathlib import Path
import re
from urllib.parse import unquote

import pytest

from car_agent.tools import TOOL_CATALOG

ROOT = Path(__file__).resolve().parents[1]
BOOK = ROOT / "docs" / "LifePlus-Agent-Harness-18问与测试验收手册.md"


@pytest.fixture(scope="module")
def convert():
    text = BOOK.read_text(encoding="utf-8")
    block = re.search(r"<!-- tested-code:flat-to-forest -->\s*```python\n(.*?)\n```", text, re.S)
    assert block, "The tested example must be present exactly once"
    assert text.count("<!-- tested-code:flat-to-forest -->") == 1
    tree = ast.parse(block.group(1))
    assert all(isinstance(node, (ast.ImportFrom, ast.FunctionDef)) for node in tree.body)
    assert [node.name for node in tree.body if isinstance(node, ast.FunctionDef)] == ["flat_to_forest"]
    assert {node.module for node in tree.body if isinstance(node, ast.ImportFrom)} == {"copy", "collections.abc"}
    namespace = {}
    exec(compile(tree, str(BOOK) + ":flat-to-forest", "exec"), namespace)
    return namespace["flat_to_forest"]


def test_empty_forest(convert):
    assert convert([]) == []


def test_unordered_children_multiple_roots_and_sibling_order(convert):
    rows = [
        {"id": 3, "parent_id": 1, "name": "first child"},
        {"id": 9, "parent_id": None},
        {"id": 2, "parent_id": 1, "name": "second child"},
        {"id": 1, "parent_id": None},
        {"id": 4, "parent_id": 3},
    ]
    before = deepcopy(rows)
    roots = convert(rows)
    assert [root["id"] for root in roots] == [9, 1]
    assert [node["id"] for node in roots[1]["children"]] == [3, 2]
    assert roots[1]["children"][0]["children"][0]["id"] == 4
    assert rows == before


def test_zero_id_is_a_real_parent_when_root_is_none(convert):
    roots = convert([{"id": 1, "parent_id": 0}, {"id": 0, "parent_id": None}])
    assert roots[0]["id"] == 0 and roots[0]["children"][0]["id"] == 1


def test_explicit_zero_root_sentinel(convert):
    roots = convert([{"id": 1, "parent_id": 0}, {"id": 2, "parent_id": 1}], root_parent=0)
    assert roots[0]["id"] == 1 and roots[0]["children"][0]["id"] == 2


def test_string_ids_are_not_coerced_to_integer_ids(convert):
    roots = convert([{"id": 1, "parent_id": None}, {"id": "1", "parent_id": 1}])
    assert roots[0]["children"][0]["id"] == "1"


def test_nested_payload_is_not_shared_with_input(convert):
    rows = [{"id": "a", "parent_id": None, "metadata": {"tags": ["original"]}}]
    roots = convert(rows)
    roots[0]["metadata"]["tags"].append("changed")
    assert rows[0]["metadata"]["tags"] == ["original"]


@pytest.mark.parametrize("rows", [
    [{"id": 1, "parent_id": None}, {"id": 1, "parent_id": None}],
    [{"id": 2, "parent_id": 99}],
    [{"id": 1, "parent_id": 1}],
    [{"id": 1, "parent_id": 2}, {"id": 2, "parent_id": 1}],
    [{"id": 9, "parent_id": None}, {"id": 1, "parent_id": 2}, {"id": 2, "parent_id": 1}],
    [{"id": 1}],
    [{"parent_id": None}],
    [{"id": True, "parent_id": None}],
    [{"id": 1, "parent_id": False}],
    [{"id": 1.0, "parent_id": None}],
    [{"id": "", "parent_id": None}],
    [{"id": "  ", "parent_id": None}],
    [{"id": 1, "parent_id": []}],
    [{"id": None, "parent_id": None}],
    [{"id": 1, "parent_id": None, "children": []}],
    [None],
])
def test_invalid_nodes_fail_instead_of_silently_losing_data(convert, rows):
    with pytest.raises(ValueError):
        convert(rows)


@pytest.mark.parametrize("items", [None, {}, (), "[]"])
def test_non_list_input_is_rejected(convert, items):
    with pytest.raises(TypeError):
        convert(items)


@pytest.mark.parametrize("sentinel", [False, [], ""])
def test_invalid_root_sentinel_is_rejected(convert, sentinel):
    with pytest.raises(ValueError):
        convert([], root_parent=sentinel)


def test_real_id_cannot_collide_with_explicit_root_sentinel(convert):
    with pytest.raises(ValueError):
        convert([{"id": 0, "parent_id": 0}], root_parent=0)


def test_deep_chain_is_built_without_recursive_walk(convert):
    count = 5000
    rows = [{"id": i, "parent_id": i - 1 if i else None} for i in reversed(range(count))]
    roots = convert(rows)
    assert len(roots) == 1
    current, visited = roots[0], 0
    while True:
        assert current["id"] == visited
        visited += 1
        if not current["children"]:
            break
        assert len(current["children"]) == 1
        current = current["children"][0]
    assert visited == count


def test_local_markdown_links_and_explicit_anchors_exist():
    text = BOOK.read_text(encoding="utf-8")
    anchors = re.findall(r'<a id="([^"]+)"></a>', text)
    assert len(anchors) == len(set(anchors)), "Duplicate anchors make the merged index ambiguous"
    for target in re.findall(r"\]\(([^)]+)\)", text):
        if target.startswith(("https://", "http://", "mailto:")):
            continue
        file, _, fragment = unquote(target.strip("<>")).partition("#")
        if file:
            assert (BOOK.parent / file).exists(), target
        elif fragment:
            assert fragment in anchors, target


def test_all_image_topics_have_a_detailed_destination():
    text = BOOK.read_text(encoding="utf-8")
    for anchor in ("io-chain", "loop-control", "execution-boundary", "no-progress", "memory-repair",
                   "state-sharing", "resource-concurrency", "restart-matrix", "hitl-outcomes", "large-result",
                   "functioncall-mcp", "rag-agent", "security-boundary", "metrics-online", "badcase-triage", "sse-contract", "appendix-f"):
        assert f'<a id="{anchor}"></a>' in text
    assert f"当前实际注册 {len(TOOL_CATALOG)} 个工具" in text
    assert "当前没有任意文件编辑工具" in text
    assert "没有 BM25、混合召回" in text
    assert "不启用提速实验" in text
