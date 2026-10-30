"""Archive and append the direct-LLM versus pure-resume-filter experiment."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from .jev_ab import summarize
from .jev_archive import RESULT_FIELDS, csv_details, json_lines, json_text, metric_changes, validate_pairs
from .jev_front_report import comparison

EXTRA_FIELDS = {"expected_filter", "actual_filter", "filter_label_correct", "unsafe_shortcut"}
START = "<!-- resume-filter-v1:start -->"
END = "<!-- resume-filter-v1:end -->"


def archive_run(source, destination, cases):
    original = [json.loads(line) for line in (source / "results.jsonl").read_text().splitlines()]
    rows = [{k: v for k, v in row.items() if k in RESULT_FIELDS | EXTRA_FIELDS} for row in original]
    validate_pairs(cases, rows, len(cases))
    manifest = json.loads((source / "manifest.json").read_text())
    if isinstance(manifest.get("cases", [None])[0], dict) and manifest["cases"] != cases:
        raise ValueError("Module frozen cases differ from original run")
    public_manifest = {k: v for k, v in manifest.items() if k not in {"candidate_url"}}
    public_manifest["source_run"] = str(source)
    if cases and isinstance(manifest.get("cases", [None])[0], str):
        public_manifest["arm_conditions"] = {}
        for arm in ("baseline", "candidate"):
            child = json.loads((source / (manifest["cases"][0] + "-" + arm) / "manifest.json").read_text())
            public_manifest["arm_conditions"][arm] = {k: child.get(k) for k in (
                "domain_route_provider", "input_event_provider", "planning_gate_provider", "model", "role_models",
                "source_sha256", "case_budget_seconds", "concurrency")}
    destination.mkdir(parents=True, exist_ok=True)
    outputs = {"cases.jsonl": json_lines(cases), "results.jsonl": json_lines(rows),
        "manifest.json": json_text(public_manifest), "summary.json": json_text(summarize(rows)),
        "comparison.json": json_text(metric_changes(summarize(rows))), "details.csv": csv_details(cases, rows)}
    for filename in ("cases.jsonl", "results.jsonl"):
        path = destination / filename
        if path.exists() and path.read_text() != outputs[filename]:
            raise ValueError("Frozen evidence changed; choose a new archive version")
    for filename, value in outputs.items():
        (destination / filename).write_text(value, encoding="utf-8", newline="")
    return rows


def append_section(path, lines):
    text = path.read_text()
    section = START + "\n" + "\n".join(lines) + "\n" + END
    if START in text or END in text:
        if text.count(START) != 1 or text.count(END) != 1 or text.index(START) > text.index(END):
            raise ValueError("Malformed experiment markers")
        prefix, rest = text.split(START, 1)
        _, suffix = rest.split(END, 1)
        text = prefix + section + suffix
    else:
        text += "\n\n" + section + "\n"
    path.write_text(text)


def comparison_tables(rows):
    return [comparison("全部事件36条", rows)] + [comparison(label, [r for r in rows if r["group"] == group])
        for group, label in (("pure_reply", "纯旧任务回复12条"), ("compound", "恢复＋新需求12条"), ("boundary", "边界12条"))]


def token_note(rows, group, label):
    arms = summarize([r for r in rows if r["group"] == group])["arms"]
    a, b = arms["baseline"], arms["candidate"]
    before, after = a["total_tokens"], b["total_tokens"]
    if before and after is None and b["observed_total_tokens"] > before:
        return (f"{label}A总Token为{before}，B已知下界{b['observed_total_tokens']}、另有{b['attempts_missing_usage']}次未知usage，"
                f"因此至少增加{(b['observed_total_tokens']/before-1)*100:.2f}%；这是消耗增加的下界，不是精确总量。")
    if before and after is not None:
        return f"{label}Token为{before}→{after}，变化{(after/before-1)*100:+.2f}%。"
    return f"{label}有未知usage，不能给出精确Token变化率。"


def current_rule_control(cases):
    from car_agent.freshness import utcnow
    from car_agent.input_events import rule_decision
    from .input_event_cases import grade, make_waiting

    counts = {}
    for case in cases:
        waiting = make_waiting(case, utcnow())
        if case.get("expired"):
            for row in waiting:
                pending = json.loads(row["interrupt_json"])
                pending["expires_at"] = "2020-01-01T00:00:00+00:00"
                row["interrupt_json"] = json.dumps(pending)
        result = rule_decision(case["query"], waiting, ["杭州", "无锡", "南京"], case["focus"])
        group = counts.setdefault(case["group"], {"submitted": 0, "handled": 0, "handled_correct": 0})
        group["submitted"] += 1
        group["handled"] += int(result is not None)
        group["handled_correct"] += int(result is not None and grade(case, result)["passed"])
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local", type=Path, required=True)
    parser.add_argument("--global-run", type=Path, required=True)
    parser.add_argument("--archive", type=Path, default=Path("evaluations/jev/2026-10-29-resume-filter"))
    parser.add_argument("--document", type=Path, default=Path("docs/jev-front-ab-2026-10-29.md"))
    args = parser.parse_args()
    datasets = args.archive / "datasets"
    cases = [json.loads(line) for line in (datasets / "events.jsonl").read_text().splitlines()]
    e2e_cases = [json.loads(line) for line in (datasets / "end_to_end.jsonl").read_text().splitlines()]
    if json.loads((args.global_run / "manifest.json").read_text())["dataset_sha256"] != hashlib.sha256((datasets / "end_to_end.jsonl").read_bytes()).hexdigest():
        raise ValueError("End-to-end frozen dataset no longer matches the trial")
    local = archive_run(args.local, args.archive / "modules/events", cases)
    global_rows = archive_run(args.global_run, args.archive / "end_to_end", e2e_cases)
    local_summary, global_summary = summarize(local), summarize(global_rows)
    rule_control = current_rule_control(cases)
    global_stages = {arm: {stage: {"calls": len(values := [a for r in global_rows if r["arm"] == arm
        for a in r["attempts"] if a["stage"] == stage]), "attempt_duration_sum_ms": sum(a["elapsed_ms"] for a in values)}
        for stage in ("input_events", "input_events_precheck", "domain_route", "response")}
        for arm in ("baseline", "candidate")}
    candidate = [row for row in local if row["arm"] == "candidate"]
    example = {row["arm"]: row for row in local if row["id"] == "pure01"}
    labeled = [row for row in candidate if row.get("actual_filter")]
    pure = [row for row in candidate if row["group"] == "pure_reply"]
    safety = {"unsafe_shortcuts": sum(row["unsafe_shortcut"] for row in candidate),
        "non_pure_submissions": sum(row["group"] != "pure_reply" for row in candidate),
        "raw_filter_correct": sum(row["filter_label_correct"] for row in labeled), "raw_filter_returned": len(labeled),
        "pure_reply_accepted": sum(row.get("source") == "jev" for row in pure), "pure_reply_submitted": len(pure),
        "jev_attempts": sum(a.get("provider") == "typesafe" for r in candidate for a in r["attempts"]),
        "llm_attempts": {arm: sum(a.get("stage") == "input_events" for r in local if r["arm"] == arm for a in r["attempts"])
                         for arm in ("baseline", "candidate")}}
    failures = [{"id": r["id"], "arm": r["arm"], "source": r.get("source"), "group": r["group"],
        "errors": r.get("grade", {}).get("errors", r.get("failures", [])), "error_type": r.get("error_type"),
        "decision": r.get("decision"), "prechecks": r.get("prechecks"),
        "attempts": [{k: a.get(k) for k in ("stage", "provider", "elapsed_ms", "error_type")} for a in r["attempts"]]}
        for r in local + global_rows if not r["passed"]]
    report = {"scope": "llm_direct_vs_jev_resume_filter_v1", "local": local_summary, "end_to_end": global_summary,
        "safety_and_coverage": safety, "current_rule_control_no_model_calls": rule_control,
        "global_stage_diagnostics": global_stages, "failures": failures, "production_changed": False}
    report["local_groups"] = {group: summarize([r for r in local if r["group"] == group])
        for group in ("pure_reply", "compound", "boundary")}
    (args.archive / "summary.json").write_text(json_text(report))
    lines = ["## 纯旧任务回复过滤实验：直接LLM vs Jev过滤＋LLM", "",
        "日期：2026-10-29。A=`INPUT_EVENT_PROVIDER=llm_direct`，主输入框的live自由文本不走原事件快规则，直接由原LLM输出InputDecision；B=`jev_resume_filter`，只对纯旧任务回复尝试跳过该LLM。卡片索引、确认、拒绝和取消仍走原宿主协议。卡片中手动输入的文本仍是文本，不是结构化按钮。", "",
        "两组都保留原文覆盖、状态/候选有效期、任务归属和权限校验。B先判断resume_only/needs_llm：只有高分resume_only且宿主能够唯一绑定整句话时，才恢复旧任务。携带任何新需求、否定、授权、取消或不确定含义，一律仍需原LLM；Jev不生成新任务片段。",
        "例如‘无锡校区吧’可只恢复旧任务；‘无锡校区吧，顺便查下明天无锡天气’必须由LLM拆成恢复＋新需求。新需求由现有API创建独立业务任务，若本身多目标再由Coordinator拆子任务，不虚称为直接往旧图中插入一个节点。", "",
        "### 冻结数据与测试条件", "",
        "- 新建36条：纯回复12、复合回复12、边界12；边界含过期、多列表歧义、否定、引用、取消、非法序号及待审批。标注在真实调用前冻结，未根据结果改提示词、门槛或标签。",
        "- 新建5个真实全链路场景：歌曲纯选择、天气补城市、两种恢复＋新任务、一个结构化卡片对照。文本回复从主输入框提交，不借用卡片focus作弊。",
        "- 原模型grok-4.3、Jev固定jev-1.14.0；门槛confidence≥0.8、概率≥0.9、Jev超时1.5秒、事件总预算25秒，回退/修复调用都计入。",
        "- 两组全局均固定领域jev_cascade、数量门控rules，仅改变事件策略。当前本机数量门控本来就是rules，本轮未启用或测试新的数量策略。",
        "- 随机场景顺序，交替AB/BA，每例一次；先运行局部、结束后才运行全局，避免两类runner争抢供应商额度。手工构造小样本，有模板复用，不代表线上SLA。",
        "- 局部只测试真实模型与事件契约，不调用业务工具；全局使用真实模型、Graph、MCP、隔离存储与自动用户回复，不包含人工思考/浏览器渲染。确认类数据的clarify是当前保守策略，不是唯一可行产品策略。", "",
        "- 局部‘南京校区/无锡校区’是人工候选快照，不是本轮高德实时回执；5例真实链路覆盖天气和音乐，不宣称已经覆盖高校实际POI简称消歧。宿主只接受完整匹配的候选/序号和有限城市回复，无法安全绑定的简称、自由起点等仍回退LLM。", "",
        "### 局部完整事件结果", "", "| 范围 | 完整通过/提交 | 准确率/完成率 | P50（秒） | P95（秒） | Token总计 |",
        "|---|---|---|---|---|---|", *comparison_tables(local), "",
        "完整正确要求route、任务ID、候选索引、旧回复与全部新需求片段正确；不是仅看Jev二分类标签。失败和超时不剔除。A原本规则能处理的纯回复也调用LLM，因此不能把这张表的收益说成相对当前规则优先系统的收益。", "",
        token_note(local, "pure_reply", "纯回复组") + token_note(local, "compound", "复合组") + "未知usage不得当作0。", "",
        f"补充零模型规则对照：现有规则在同一12条纯回复中直接处理{rule_control['pure_reply']['handled']}条，其中正确{rule_control['pure_reply']['handled_correct']}条。未命中的部分没有在这个对照中调用LLM，因此不提供伪造的规则系统整体时延或准确率。这个对照只说明直接LLM不是当前服务基线，不是额外一轮付费A/B。", "",
        "### Jev覆盖与安全边界", "",
        f"- 纯回复接管：{safety['pure_reply_accepted']}/{safety['pure_reply_submitted']}；其余回退原LLM。",
        f"- 实际返回二分类的请求：{safety['raw_filter_returned']}；其中标签匹配期望{safety['raw_filter_correct']}，分母不包含没调用或服务失败的请求。这个标签正确率不替代全系统通过率。",
        f"- 复合/边界误走纯恢复捷径：{safety['unsafe_shortcuts']}/{safety['non_pure_submissions']}。这是本批受测次数，不保证未来永久为0。",
        f"- 事件原LLM尝试次数：{safety['llm_attempts']['baseline']} → {safety['llm_attempts']['candidate']}；候选还调用Jev {safety['jev_attempts']}次。减少原LLM不等于所有模型调用数或Token都减少。", "",
        "这次局部只有2条被Jev接管明确省去事件LLM；原LLM尝试次数其余差异还包括A组格式修复次数，不能全部当成Jev带来的节省。", "",
        f"局部例子‘无锡校区吧’：{example['baseline']['elapsed_ms']/1000:.3f}→{example['candidate']['elapsed_ms']/1000:.3f}秒，"
        f"候选实际来源为{example['candidate'].get('source', 'error')}。单例收益不能代表整个纯回复组，未过既定门槛仍回退；测试期间没有为了让结果更好而降低阈值。", "",
        "### 真实全链路结果", "", "| 范围 | 完整通过/提交 | 准确率/完成率 | P50（秒） | P95（秒） | Token总计 |",
        "|---|---|---|---|---|---|", comparison("5个完整会话", global_rows), "",
        "时延包含初始创建任务、真实工具、文本事件解释、恢复及所有新任务完成，不只是回复过滤耗时；两组初始新请求都需要原事件LLM。结构化卡片对照仍包含初始模型耗时，不把整场会话误称为零模型。", "",
        f"**关键归因：** 5组全局的事件LLM调用次数为{global_stages['baseline']['input_events']['calls']}→{global_stages['candidate']['input_events']['calls']}，"
        f"B额外调用事件Jev {global_stages['candidate']['input_events_precheck']['calls']}次。原领域LLM回退次数为"
        f"{global_stages['baseline']['domain_route']['calls']}→{global_stages['candidate']['domain_route']['calls']}；回复模型耗时也有波动。"
        "因此不能仅凭全局P50变化宣称过滤器节省了事件LLM，需结合下面的阶段记录。", "",
        "| 阶段 | A调用数 / 尝试时长合计 | B调用数 / 尝试时长合计 |", "|---|---:|---:|"]
    for stage in ("input_events", "input_events_precheck", "domain_route", "response"):
        a, b = global_stages["baseline"][stage], global_stages["candidate"][stage]
        lines.append(f"| {stage} | {a['calls']} / {a['attempt_duration_sum_ms']/1000:.3f}秒 | {b['calls']} / {b['attempt_duration_sum_ms']/1000:.3f}秒 |")
    lines += ["", "时长合计跨多个会话及模型尝试，仅用于定位工作量，不是单请求P50/P95，也不将并行分支相加当作端到端时延。", "",
        "### 失败证据与解释", ""]
    if failures:
        lines += ["- `" + f["id"] + "/" + f["arm"] + "`：`" + json.dumps(f, ensure_ascii=False) + "`。" for f in failures]
    else:
        lines += ["本批完整事件与全链路没有失败；仅表示本批数据，不意味着所有真实表达均正确。"]
    lines += ["", "本轮边界请求的通过数差异如发生在原LLM解析、超时或连接阶段，不应解读为Jev二分类修好了这些问题；两组都使用同一个原LLM，单次采样存在波动。"]
    lines += ["", "### 维护与复跑", "",
        "本轮只产出证据，不自动改主输入框策略或启用过滤器。检查分组耗时、全部Token、接管率及原规则覆盖后再做启用决策；整体偶然更快不能替代对事件阶段的归因。后续可独立比较‘规则已能完整绑定就直接恢复、真正需要语义判断时再用Jev’，但不把未经测试的改动写成已生效。", "",
        "本实验不切换本机/容器默认，不自动更新GitHub。已启用的单领域Jev和其他服务配置保持不变。低分回退、服务异常和原LLM错误分别归因，不把回退错误说成Jev二分类错误。未知usage记null/下界，不能据此推断费用节省。", "",
        "[本实验数据与逐条证据](../evaluations/jev/2026-10-29-resume-filter/README.md)。原始audit SQLite和全局Trace保留在对应evaluations/runs。", "",
        "```bash", ".venv/bin/python -m evaluations.jev_event_ab --strategy jev_resume_filter --baseline-policy llm_direct --dataset evaluations/jev/2026-10-29-resume-filter/datasets/events.jsonl --output evaluations/runs/<new-local-run>",
        ".venv/bin/python -m evaluations.jev_global_ab --candidate resume_filter --dataset evaluations/jev/2026-10-29-resume-filter/datasets/end_to_end.jsonl --output evaluations/runs/<new-global-run>",
        "```", "", "上述命令调用真实付费API，使用新目录，不覆盖旧记录。报告导出不发起模型调用。"]
    append_section(args.document, lines)
    readme = ["# 纯回复过滤A/B：2026-10-29 v1", "", "主测试文档：[持续维护记录](../../../docs/jev-front-ab-2026-10-29.md)。", "",
        "- `datasets/events.jsonl`：36条输入、待回复任务、期望事件及期望二分类。",
        "- `datasets/end_to_end.jsonl`：5个完整会话的目标及用户模拟器策略。",
        "- `modules/events/`：模块cases/results JSONL、CSV、manifest、summary与变化量。",
        "- `end_to_end/`：5组全局真实模型/MCP回执验证的可共享结果，移除归属与内部路径。",
        "- `summary.json`：两组质量、P50/P95、全部Token、二分类覆盖、安全分母及失败归因。", "",
        "报告复算：`python -m evaluations.resume_filter_report --local <原局部run目录> --global-run <原全局run目录>`。只处理现有证据，不调用模型；归档cases/results冻结，内容不一致拒绝覆盖。", "",
        "A是自然文本直接LLM，不是当前规则优先基线。B只允许纯旧任务回复跳过LLM，不负责拆分复合事件。领域Jev和原数量规则固定一致。36个局部场景与5个全局场景分别报告，不能合并成41个独立线上会话或混算准确率。", "",
        "所有失败、回退、未知usage保留；不包含原SQLite、认证或Key。原数据不修改，候选默认不启用。"]
    (args.archive / "README.md").write_text("\n".join(readme) + "\n")
    print(json.dumps({"local_pairs": len(cases), "global_pairs": len(e2e_cases), "archive": str(args.archive), "model_calls": 0}))


if __name__ == "__main__":
    main()
