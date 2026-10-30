"""Keep pre-fix failures and post-fix verification as separate immutable evidence."""
import json
from pathlib import Path

from .current_measurements import aggregate, clean_completion, frozen
from .robustness_report import assertions, tokens

TAIL = Path("evaluations/runs/robustness-fix30-20261030-01")
E2E = Path("evaluations/runs/robustness-fix-completion90-20261030-01")
EVENTS = Path("evaluations/runs/robustness-fix-events36-20261030-01")
RAG = Path("evaluations/runs/robustness-fix-rag-20261030-01.json")
PUBLIC = Path("测试数据集/结果记录/2026-10-30-badcase-fix-01")


def read(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load(output, root):
    target = output / PUBLIC
    if all((root / path / "summary.json").exists() for path in (TAIL, E2E, EVENTS)) and (root / RAG).exists():
        tail, e2e, events = (json.loads((root / path / "summary.json").read_text()) for path in (TAIL, E2E, EVENTS))
        if not tail["complete"] or not events["complete"] or e2e["tested"] != 90:
            return None
        complete = clean_completion(root / E2E)
        value = {"model": "deepseek-flash", "thinking": "disabled", "tail30": tail,
            "full90": aggregate(complete), "events36": events,
            "rag": json.loads((root / RAG).read_text()),
            "contract_assertions": assertions(root / TAIL / "contracts.xml"),
            "scope": "sequential_regression_not_randomized_AB; post_fix_not_deployed"}
        frozen(target / "完整90会话逐条结果.json", complete)
        frozen(target / "完整90会话运行条件.json", json.loads((root / E2E / "manifest.json").read_text()))
        for name, path in (("长尾30条", TAIL), ("事件36条", EVENTS)):
            frozen(target / f"{name}逐条结果.json", read(root / path / "results.jsonl"))
            frozen(target / f"{name}运行条件.json", json.loads((root / path / "manifest.json").read_text()))
        frozen(target / "RAG当前源码容器验证.json", value["rag"])
        frozen(target / "故障断言明细.json", value["contract_assertions"])
        frozen(target / "汇总.json", value)
    else:
        retained = root / "测试回报" / PUBLIC
        source = retained if (retained / "汇总.json").exists() else target
        if not (source / "汇总.json").exists():
            return None
        value = json.loads((source / "汇总.json").read_text())
        for file in source.glob("*.json"):
            frozen(target / file.name, json.loads(file.read_text()))
    (target / "README.md").write_text(
        "# 事件、绑定及时效、RAG修复复测\n\n修复前证据在2026-10-30-robustness-01，本目录只追加，不覆盖失败。"
        "同一DeepSeek关闭思考，先完整90、再17条模型组件与受控场景、再36事件；不是随机配对A/B，不能宣布纯因果提速。"
        "30条仍为17模型组件＋13受控，不能称30条端到端；36与5条事件有Query/机制复用。\n\n"
        "RAG把当前源码加载到现有容器独立模块，以临时真实Chroma验证；没有替换已运行服务文件、重启服务或修改正常知识库。"
        "host无Chroma的审计仍blocked，不能当作第二组成功索引。\n\n"
        "汇总、逐条prediction/grade、运行条件及源文件hash用于复核。未知Token保留null及下界，控制层测试不参与模型成本和会话延迟。"
        "本轮仅关闭受测边界，不声称覆盖绑定后长等待时效、多实例索引切换、生产公网认证或所有自然语言表达。\n")
    return value


def section(value, before=None):
    if not value:
        return ""
    old = before["tail30"]["by_layer"] if before else {}
    new = value["tail30"]["by_layer"]
    lines = ["## 0.3 本轮 Bad Case 修复与复测", "",
        "保持DeepSeek关闭思考，不启用文本规则抢答或Jev事件前置；修改事件提示、规划静态来源约束、绑定时效/类型校验和Chroma旧ID清理。"
        "说明与约三分钟面试回答集中在[bad case解决专章](#badcase-fix-20261030)。未部署或重启正常服务。", "",
        "| 验证层 | 修复前→后通过数 | 修复后P50/P95秒 | 修复后Token |", "|---|---|---|---|"]
    for layer, label in (("event_live", "新增事件"), ("planner_live", "新增规划"), ("domain_live", "领域参数"), ("controlled_contract", "受控场景")):
        item = new[layer]
        prior = f"{old[layer]['passed']}/{old[layer]['submitted']}" if old else "未载入"
        latency = "受控执行时间，不作会话指标" if layer == "controlled_contract" else f"{item['p50_ms']/1000:.3f}/{item['p95_ms']/1000:.3f}"
        lines.append(f"| {label} | {prior} → {item['passed']}/{item['submitted']} | {latency} | {tokens(item) if layer != 'controlled_contract' else '无模型调用'} |")
    events, full = value["events36"]["by_layer"]["event_live"], value["full90"]
    lines += [f"| 原36条事件回归 | 34/36 → {events['passed']}/{events['submitted']} | {events['p50_ms']/1000:.3f}/{events['p95_ms']/1000:.3f} | {tokens(events)} |",
        f"| 完整90会话 | 90/90 → {full['passed']}/{full['submitted']} | {full['p50_ms']/1000:.3f}/{full['p95_ms']/1000:.3f} | {tokens(full)} |", "",
        f"完整会话目标{full['goals_completed']}/{full['goals_total']}。RAG删除/缩短在临时真实Chroma由0/2变2/2，旧条款不可召回，受测当前源码hash匹配。"
        "新增数据没有事后改gold，所有提交、错误尝试与用量保留；事件和规划局部通过不等于全系统准确率。", "",
        "**代价与归因：** 事件提示更完整也增加输入Token；旧36事件从24659增至28183，本轮目的为纠正边界而非证明省Token。"
        "多个改动共同验证，没有消融或新随机配对，不把历史时延差异全归给某一行提示词。完整90每轮均单并发和自动用户回复，未知usage不按零。", "",
        "**本轮关闭的范围：** 唯一取消/词义提问的受测事件；对象→标量和来源字段的计划门禁；实际值存在性/严格类型与绑定入场时效；"
        "当前知识集合在显式open时的旧ID差集清理。用户确认参数变化、未知写入不重试等旧受控门禁仍通过。", "",
        "**不夸大的边界：** _bind过期拒绝是入场门，不代表所有绑定后长时间等待都已覆盖；无时效元数据的兼容快照不自动等于新鲜。"
        "RAG不是文件监听或多实例原子版本切换。尚未补新的未见生产留出集、任意事件表达、跨实例灾备或公网压测。", "",
        f"[本轮逐条结果、源码hash与条件]({PUBLIC.as_posix()}/README.md)。第0.2节及原失败记录保持原值，用于追溯发现和修复过程。", ""]
    return "\n".join(lines)
