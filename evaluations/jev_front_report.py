"""Rebuild the three-module pilot report from retained provider evidence."""
import json
from collections import Counter
from pathlib import Path

from .jev_ab import summarize, usage_summary
from .jev_archive import ARCHIVE, LAYOUT, export_archive


ROOT = Path("evaluations/runs")
RUNS = {
    "domain": ("jev-front-domain-20261029-01", 21),
    "cardinality": ("jev-cardinality-20261029-01", 30),
    "events_dev": ("jev-event-gate-dev-20261029-01", 30),
    "events_holdout": ("jev-event-gate-holdout-20261029-01", 12),
    "global": ("jev-front-global-20261029-01", 12),
    "boundaries": ("jev-front-boundaries-global-20261029-01", 4),
}


def load(name):
    directory, count = RUNS[name]
    archived = ARCHIVE / LAYOUT[name][0] / "results.jsonl"
    path = archived if archived.exists() else ROOT / directory / "results.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    if len(rows) != count * 2 or any(sum(r["arm"] == arm for r in rows) != count for arm in ("baseline", "candidate")):
        raise ValueError("Incomplete evidence: " + name)
    return rows


def amount(arm):
    return str(arm["total_tokens"]) if arm["total_tokens"] is not None else f"≥{arm['observed_total_tokens']}（{arm['attempts_missing_usage']}次未知）"


def comparison(name, rows):
    arms = summarize(rows)["arms"]
    a, b = arms["baseline"], arms["candidate"]
    return (f"| {name} | {a['passed']}/{a['tested']} → {b['passed']}/{b['tested']} | "
            f"{a['accuracy_or_completion_rate']*100:.2f}% → {b['accuracy_or_completion_rate']*100:.2f}% | "
            f"{a['p50_ms']/1000:.3f} → {b['p50_ms']/1000:.3f} | {a['p95_ms']/1000:.3f} → {b['p95_ms']/1000:.3f} | "
            f"{amount(a)} → {amount(b)} |")


START = "<!-- jev-ab:generated:start -->"
END = "<!-- jev-ab:generated:end -->"


def write_maintained_report(path, lines):
    text = path.read_text() if path.exists() else ""
    if "<!-- lifeplus-test-report:redirect -->" in text:
        raise ValueError("Report migrated; use python -m evaluations.test_report")
    if START in text or END in text:
        if text.count(START) != 1 or text.count(END) != 1 or text.index(START) > text.index(END):
            raise ValueError("Invalid generated section markers; refusing to overwrite maintained notes")
        prefix, rest = text.split(START, 1)
        _, suffix = rest.split(END, 1)
    else:
        prefix = ""
        suffix = "\n\n## 后续测试记录（人工维护）\n\n此区保留人工备注；报告重建不会覆盖。新增试验记录日期、范围、数据版本、结论与证据目录，不改写历史实测值。\n"
    path.write_text(prefix + START + "\n" + "\n".join(lines) + "\n" + END + suffix)


def domain_overview(summary):
    a, b = summary["arms"]["baseline"], summary["arms"]["candidate"]
    lines = ["## 单任务领域仲裁（已启用）", "",
        "范围：相同21条冻结领域样本；A为原`grok-4.3`领域分类，B为`jev-1.14.0`高置信度接受、其余回退同一原模型。下面统计B的全部尝试，不只统计Jev接受的快样本。", "",
        "| 指标 | 原方案A | Jev级联B | 变化 |", "|---|---:|---:|---|"]
    lines += [f"| 完整通过/提交 | {a['passed']}/{a['tested']} | {b['passed']}/{b['tested']} | 正确数不变 |",
        f"| 领域准确率 | {a['accuracy_or_completion_rate']*100:.2f}% | {b['accuracy_or_completion_rate']*100:.2f}% | {(b['accuracy_or_completion_rate']-a['accuracy_or_completion_rate'])*100:+.2f}个百分点，持平 |"]
    for key, label in (("p50_ms", "P50耗时"), ("p95_ms", "P95耗时")):
        saved = a[key] - b[key]
        lines.append(f"| {label} | {a[key]/1000:.3f}秒 | {b[key]/1000:.3f}秒 | 减少{saved/1000:.3f}秒，下降{saved/a[key]*100:.2f}% |")
    before, after = a["total_tokens"], b["total_tokens"]
    if before is None or after is None:
        raise ValueError("Domain headline requires complete provider usage")
    lines += [f"| 总Token | {before:,} | {after:,} | 增加{after-before:,}，上升{(after-before)/before*100:.2f}% |",
        f"| 平均每提交样本Token | {before/a['tested']:.2f} | {after/b['tested']:.2f} | 增加{after/b['tested']-before/a['tested']:.2f} |", "",
        "准确率 = 正确领域样本数 / 全部提交样本数；异常和超时计失败，回退后的正确结果计入级联系统准确率。P50/P95按nearest-rank计算，均包含失败与回退耗时。耗时下降率 = (A−B)/A；Token变化率 = (B−A)/A。",
        "**结论：领域分类更快、准确率持平，但总Token增加，并未节省Token，也没有证明账单费用降低。** 不能把领域阶段84.57%的P50下降当作整条业务请求提速；21/21是此人工样本集结果，不是生产100%准确率。", "",
        f"B共有{b['model_attempts']}次模型尝试：Jev分类{b['by_stage']['domain_route_precheck']['model_attempts']}次、原领域模型回退{b['by_stage'].get('domain_route', {}).get('model_attempts', 0)}次。总Token包含这些回退。Jev单独接受样本的准确率与本表的最终级联准确率不是同一个分母。", "",
        "[领域输入与期望](../evaluations/jev/2026-10-29/modules/domain/cases.jsonl) · [逐条CSV](../evaluations/jev/2026-10-29/modules/domain/details.csv) · [逐条JSONL](../evaluations/jev/2026-10-29/modules/domain/results.jsonl) · [汇总](../evaluations/jev/2026-10-29/modules/domain/summary.json)", ""]
    return lines


def main():
    entry = Path("docs/jev-front-ab-2026-10-29.md")
    if entry.exists() and "<!-- lifeplus-test-report:redirect -->" in entry.read_text():
        from .test_report import build
        build()
        return
    export_archive()
    rows = {name: load(name) for name in RUNS}
    event_model = [r for r in rows["events_dev"] if r["group"] == "requires_model"]
    global_rows = rows["global"]
    summaries = {name: summarize(value) for name, value in rows.items()}
    summaries["events_dev_model_only"] = summarize(event_model)
    failures = {name: [{"id": r["id"], "arm": r["arm"], "errors": r.get("grade", {}).get("errors", r.get("failures", [])),
        "error_type": r.get("error_type"), "source": r.get("source"),
        "attempts": [{k: a.get(k) for k in ("stage", "provider", "elapsed_ms", "error_type")} for a in r["attempts"]]}
        for r in values if not r["passed"]] for name, values in rows.items()}
    pairs = {}
    for row in global_rows:
        pairs.setdefault(row["id"], {})[row["arm"]] = row
    common_known = [p for p in pairs.values() if all(usage_summary(r["attempts"])["total_tokens"] is not None for r in p.values())]
    complete_pair_usage = {arm: usage_summary([a for p in common_known for a in p[arm]["attempts"]]) for arm in ("baseline", "candidate")}
    evidence = {"scope": "authored_real_provider_pilot_not_production", "default_changed": False,
        "summaries": summaries, "failures": failures,
        "common_known_global": {"case_ids": [p["baseline"]["id"] for p in common_known], "usage": complete_pair_usage},
        "global_case_results": [{k: r[k] for k in ("id", "arm", "passed", "elapsed_ms", "failures", "precheck_counts")} for r in global_rows],
        "source_runs": {name: str(ROOT / value[0]) for name, value in RUNS.items()}}
    evidence["archive"] = str(ARCHIVE)
    lines = ["# Jev 优化测试记录（持续维护）", "",
        "测试基线日期：2026-10-29。原模型 `grok-4.3`；决策模型 `jev-1.14.0`。本文集中记录模块与全链路的正确率、耗时、Token、失败及边界；小样本结果不代表生产SLA。", "",
        "**当前启用范围：** 经用户确认，仅启用`DOMAIN_ROUTE_PROVIDER=jev_cascade`；事件解释仍为`llm`、单/多任务门控仍为`rules`。[启用与本机验收](jev-domain-rollout.md)。本次维护不重新调用模型；以下仍是当时冻结的实测数据。", ""]
    lines += domain_overview(summaries["domain"])
    lines += ["## 统一测试数据目录", "",
        "所有对应输入、期望、逐条结果、CSV、汇总与变化量整理在[统一归档](../evaluations/jev/2026-10-29/README.md)。原始运行目录、SQLite与完整日志继续保留，不移动、不删除。", "",
        "| 数据范围 | 文件夹 | 场景数 / A+B观察数 |", "|---|---|---:|"]
    lines += [f"| {LAYOUT[name][1]} | [{LAYOUT[name][0]}](../evaluations/jev/2026-10-29/{LAYOUT[name][0]}) | {RUNS[name][1]} / {RUNS[name][1]*2} |" for name in RUNS]
    lines += ["", "新增的[纯回复过滤A/B数据](../evaluations/jev/2026-10-29-resume-filter/README.md)独立保存36条事件与5组全链路，结果追加在本文末尾独立实验区，不与上表旧版事件审核合并统计。", "",
        "**整体测试范围必须区分：** `front_combined`的12例同时启用事件审核与合并前序判断，`front_boundaries`为另选的4例机制验证；都不是当前仅启用单目标领域Jev版本的全局A/B。该版本目前有2例Docker真实冒烟验收，但没有单独的全局配对统计，不能冒用联合方案数字。", "",
        "**维护约定：** 每次记录完整通过/提交、准确率或完成率及其分母、P50/P95、全部Token与未知尝试、配置和代码/数据版本。正向与负向结果均保留，不把多个重叠集混算为整体。自动区由脚本复算，人工备注放文末保留区。", "",
        "## 结论", "",
        "1. **单任务领域仲裁已经获批启用。** 它可以替代一次原LLM领域分类，不替代工具选择、槽位提取、澄清或结果校验。速度收益与Token收益必须分别看。",
        "2. **单/多任务判断适合继续验证。** 原实现是宽松正则粗筛，不是每次都用大模型分类；Jev增加一次低延迟模型调用，但可减少不必要的规划生成，也能识别规则漏掉的复合请求。",
        "3. **事件明确性审核暂不建议启用。** 判为明确不等于拿到了任务ID、候选和新需求片段。此次使用宿主构造完整方案、Jev审核、不通过则原LLM解释的保守路径；回退过多，不能稳定减负。",
        "4. **单次Jev可同时询问任务数量和单任务领域。** 不串行调用三个分类器；多任务仍必须由原规划器生成目标和依赖。减少原LLM调用不保证总Token下降。", "",
        "领域局部P50下降84.6%，Token增加9.3%；事件扩展留出Token增加27.0%。合并前序的普通全局12例没有提速，9个用量完整配对Token增加16.7%；针对性4例P50下降11.6%，但Token增加20.3%。这些比例仅对应各自样本，不能跨样本替代或推广到生产。", "",
        "## 实际测试方式", "",
        "- 高确定性事件规则依然优先，零模型调用的路径不应为使用Jev而增加网络请求。候选无法唯一构造、歧义、超时及低置信度回退原解释器。授权不由Jev授予。",
        "- 新开关 `INPUT_EVENT_PROVIDER=jev_gate` 用二选一审核唯一完整事件方案，与先前从多份候选选择的 `jev_cascade` 区分。",
        "- `PLANNING_GATE_PROVIDER=jev_cardinality` 只判断目标数量；`jev_front` 在同一请求增加领域问题。单目标且领域通过门槛时，将原Query与领域传递下去，不生成参数。多目标调用原GoalPlan规划器。",
        "- 固定1.5秒超时、confidence≥0.8及选中项概率≥0.9。沿用前轮门槛，没有根据本轮测试结果降阈值或修改业务提示词。概率不是未经验证即可宣称的真实准确率。",
        "- 逐例交替AB/BA，真实请求模型；全局还走HTTP处理器、LangGraph、MCP及隔离SQLite。每例每组独立进程；自动回复模拟澄清/确认，不包含真实用户思考时间，不包含浏览器/TCP渲染。",
        "- 此轮局部与全局runner有时间重叠，共享同一供应商配额，并非无干扰性能压测。保留429、超时和修复调用；小样本单次重复容易受网络与模型采样影响，不据此宣布显著提升。",
        "- 领域21条为已有人工留出样本的复测；事件30条开发与12条扩展留出包含既有6条和本次新写6条；目标数量30条为人工编写的边界压力样本，不代表线上类别分布。没有训练或调参。", "",
        "## 各模块与整体配对结果", "",
        "耗时单位为秒，均计入失败。每行使用相同配对样本；Token包含Jev、原模型回退及格式修复，不是账单费用。", "",
        "质量列对模块表示领域/数量分类正确率或完整事件契约通过率，对全局表示整场景全部目标完成率，不能混称为Jev独立准确率。", "",
        "| 范围 | 完整通过/提交 | 准确率/完成率 | P50（秒） | P95（秒） | Token总计 |", "|---|---|---|---|---|---|"]
    lines += [comparison("领域仲裁21条", rows["domain"]), comparison("单/多目标判断30条", rows["cardinality"]),
        comparison("事件开发：仅需模型的17条", event_model), comparison("事件扩展留出12条", rows["events_holdout"]),
        comparison("全局完整任务12例", global_rows), comparison("针对性链路验证4例（单独报告）", rows["boundaries"]), "",
        "**注意：** 原单/多目标判断本身只有正则，毫秒取整后为0，Token为0；Jev在这个局部是增加耗时和Token，不是提速。它的潜在收益来自后续少一次不必要的规划/路由，不可把60%粗筛命中率说成原系统端到端完成率。", "",
        "事件开发集另有13条规则命中且不调用模型，两组全部通过。混合这13条零耗时会造成总体中位数对一条快样本极敏感，因此主表只列需要模型的17条，不使用混合总体P50宣传收益。", "",
        "## 逐模块解释", "",
        "### 事件：明确性不是二元标签就足够", "",
        "例如‘第二个，帮我介绍南京天气’，正确事件必须包含：旧任务ID、第二个对应的快照、原文选择片段、完整的新需求片段。若音乐和地点都在等待且无焦点，不能猜一个。",
        "宿主先按原文边界枚举并校验方案，合并语义等价的序数/候选名称；只有唯一方案才交Jev审核。Jev返回clear也必须重新通过任务归属、候选有效期、原文覆盖及权限校验。无法构造方案则直接调用原LLM，避免一次必定无收益的Jev。", ""]
    for name in ("events_dev", "events_holdout"):
        subset = [r for r in rows[name] if r["arm"] == "candidate"]
        lines += [f"- {name}：{sum(r.get('source') == 'jev' for r in subset)}/{len(subset)}条被Jev直接接管；"
                  f"实际Jev调用{sum(a.get('stage') == 'input_events_precheck' for r in subset for a in r['attempts'])}次。规则命中与无法构造方案不能算作Jev正确分类。"]
    lines += ["", "具体失败归因见文末：若没有Jev调用或Jev已回退，后续LLM随机格式失败不能归因于Jev做错事件标签；但仍计入候选系统完整失败率。此次不以降低阈值来掩盖回退成本。", "",
        "### 单/多目标：减少误触发规划，不是取代规划", "",
        "规则把逗号、‘和’等当疑似多任务。例如‘查无锡天气，谢谢’、坐标逗号、歌曲名《你和我》、‘和平路’、单目标的先检索再导航，都容易误触发。反过来，‘我要无锡明日天气外加周杰伦的歌’可能漏召回。",
        "30条压力样本中粗筛18/30，Jev级联27/30，提升30个百分点。26条高置信度接管均正确；4条回退后有3条沿用规则误判。不是Jev高置信度把这3条判错。原规划器还可能纠正规则误触发，因此不是从60%提升到了90%的任务完成率。",
        "低置信度回退样本包括‘提醒我明早八点带雨伞和钥匙’、‘查一查明天下午无锡下不下雨以及气温’和‘无锡天气周杰伦歌曲都要’。可考虑把不确定输入送生成式规划器，而非再次依赖粗筛；这会增加模型调用，需另做预先冻结数据的A/B，当前未启用。", "",
        "### 单目标领域：最直接的一次模型替代", "",
        "单领域分类只在有限的七领域与defer中选择，宿主固定Schema。最终工具与参数继续由领域understand生成和校验；多目标计划已经分派领域的子任务不重复分类。此次21条领域样本两组全部正确，是持平，不是准确率提高。", "",
        "### 全局：不能按局部速度相加", "",
        "候选组同时开启事件审核和合并前序分类，但不改原规划器、工具、参数、确认和回复。对每个任务都有真实planning.precheck事件，使用模型attempt验证调用路径生效。并发领域分支的耗时相互重叠，不能相加后当作端到端耗时。", "",
        "12例实测虽然8/12→10/12完成，但P50反而17.820→18.538秒（增加4.0%），P95为43.658→47.566秒（增加9.0%）。不能宣布全局提速。两条完成数改善涉及原模型的事件超时与领域调用429，不等于Jev分类正确率提高16.7个百分点；需要更多隔离、重复试验。",
        "为验证减少误触发规划，另外固定4个针对性案例：带‘谢谢’的天气请求、起点坐标逗号、歌曲《你和我》、没有明显连接词的天气＋音乐。它们来自机制定位，且在最初试验后选取，存在针对性选择偏差；与原12例分开报告，不将两者混合成更好看的总体。", "",
        "| 全局阶段 | 原模型调用数 | 候选原模型调用数 | 候选Jev调用数 |", "|---|---:|---:|---:|"]
    stages = sorted({a["stage"] for r in global_rows for a in r["attempts"]})
    for stage in stages:
        counts = [Counter(a.get("provider") == "typesafe" for r in global_rows if r["arm"] == arm for a in r["attempts"] if a["stage"] == stage)
                  for arm in ("baseline", "candidate")]
        lines.append(f"| {stage} | {counts[0][False]} | {counts[1][False]} | {counts[1][True]} |")
    lines += ["", "## Token与失败口径", "",
        "优先读取供应商total_tokens，若只有input/output则相加；不重复加reasoning。任何尝试缺少usage，组总量记未知，只给已知下界，不把失败视为免费。", "",
        f"全局共同用量完整的配对有{len(common_known)}组；仅这组固定交集可精确对比："
        f"{complete_pair_usage['baseline']['total_tokens']} → {complete_pair_usage['candidate']['total_tokens']} Token。"
        "这不是全部12例的Token节省结论，未知尝试、被排除样本及其失败均保留在全体主表中。", "",
        "以下失败全部保留在主结果中，归因只用于定位，不删除难例：", ""]
    for name in ("events_dev", "events_holdout", "global", "boundaries"):
        for failure in failures[name]:
            lines.append("- `" + name + "/" + failure["id"] + "/" + failure["arm"] + "`：`" + json.dumps(failure, ensure_ascii=False) + "`。")
    lines += ["", "## 下一步与配置边界", "",
        "- 单目标领域路由已启用，继续扩大独立测试，验证长尾、过期上下文、服务抖动和错误领域的拒判；其他Jev试验不启用。",
        "- 合并数量/领域判定值得继续测试；先完善不确定输入送生成式规划器的策略，再评估Token提示词裁剪。多目标生成式规划依旧是必要环节。",
        "- 事件明确性审核的门槛与样本表现尚不支持启用；继续保持规则＋原LLM。含两处原文的新旧复合事件不能退化成只判单/多任务。",
        "- 复合事件已经需要原LLM解释时，不在前面强加必然回退的Jev；全局计划已经分配领域时，不重复做单目标领域分类。下一轮可单独测试缩短分类提示词，但不能删掉跨域和否定约束来换Token节省。",
        "- 规划超时、bind_slots非法等已有失败需单独修复和回归，不能把换分类模型当作解决这些问题。本轮为可归因对照不同时改它们。",
        "- 当前本机与配置模板为 `INPUT_EVENT_PROVIDER=llm`、`DOMAIN_ROUTE_PROVIDER=jev_cascade`、`PLANNING_GATE_PROVIDER=rules`；本地Key不进入报告、Git或Trace。", "",
        "工程回归：700通过、1跳过（`data/jev-front-tests-20261029.xml`），覆盖可选前序的单次多问题契约、保留原默认、服务失败回退、完整原文方案及禁止授权绕过。受控测试不用于宣称真实模型准确率。", "",
        "## 可复现命令", "", "```bash",
        ".venv/bin/python -m evaluations.jev_ab --run-live --split holdout --repeats 1 --output evaluations/runs/<new-domain-run>",
        ".venv/bin/python -m evaluations.jev_gate_ab --output evaluations/runs/<new-cardinality-run>",
        ".venv/bin/python -m evaluations.jev_event_ab --strategy jev_gate --output evaluations/runs/<new-event-dev-run>",
        ".venv/bin/python -m evaluations.jev_event_ab --strategy jev_gate --front-holdout --output evaluations/runs/<new-event-holdout-run>",
        ".venv/bin/python -m evaluations.jev_global_ab --candidate front --output evaluations/runs/<new-global-run>",
        ".venv/bin/python -m evaluations.jev_global_ab --candidate front --dataset evaluations/data/jev-front-boundaries-v1.jsonl --output evaluations/runs/<new-boundaries-run>",
        "```", "", "仅归档与重建报告，无付费API调用：", "", "```bash",
        ".venv/bin/python -m evaluations.jev_archive", ".venv/bin/python -m evaluations.jev_front_report", "```", "",
        "上面真实试验命令会消耗供应商额度，目录必须使用新名称。归档和报告生成器只读既有证据，不产生模型费用；没有私有原run目录时可使用统一归档复算。原始SQLite、日志和结果在evaluations/runs对应目录；共享脱敏汇总位于[JSON证据](assets/jev-front-ab-20261029.json)。"]
    Path("docs/assets/jev-front-ab-20261029.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    write_maintained_report(Path("docs/jev-front-ab-2026-10-29.md"), lines)


if __name__ == "__main__":
    main()
