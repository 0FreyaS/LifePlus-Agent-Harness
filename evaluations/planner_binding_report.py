"""Archive real binding-fix pairs without overwriting the pre-fix baseline."""
from __future__ import annotations

import json
from pathlib import Path

RUN = Path("evaluations/runs/planner-binding-fix-ab-20261030-01")
GENERAL_RUN = Path("evaluations/runs/planner-binding-general-30-20261030-01")
PUBLIC = Path("测试数据集/结果记录/2026-10-30-planner-binding-fix-01")


def frozen(path, data):
    if path.exists() and path.read_bytes() != data:
        raise ValueError("Frozen binding-fix evidence differs; create a new version")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def load(output, root):
    source, target = root / RUN, output / PUBLIC
    if (source / "summary.json").exists():
        value = json.loads((source / "summary.json").read_text())
        if not value["complete"]:
            return None
        if value["submitted"] != 74:
            raise ValueError("Expected 37 paired cases")
        for name in ("manifest.json", "cases.jsonl", "results.jsonl", "summary.json"):
            frozen(target / name, (source / name).read_bytes())
        for name in ("task_planning.py", "structured_output.py"):
            frozen(target / "baseline_source" / name, (source / "baseline_source" / name).read_bytes())
    else:
        retained = target if (target / "summary.json").exists() else root / "测试回报" / PUBLIC
        if not (retained / "summary.json").exists():
            return None
        value = json.loads((retained / "summary.json").read_text())
        for path in retained.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".jsonl", ".py"}:
                frozen(target / path.relative_to(retained), path.read_bytes())
    general = root / GENERAL_RUN
    if (general / "summary.json").exists():
        regression = json.loads((general / "summary.json").read_text())
        if regression["submitted"] == 30:
            for name in ("manifest.json", "results.jsonl", "summary.json"):
                frozen(target / "original30" / name, (general / name).read_bytes())
            value = {**value, "original30": regression}
    elif (target / "original30/summary.json").exists():
        value = {**value, "original30": json.loads((target / "original30/summary.json").read_text())}
    engineering = root / "测试回报" / PUBLIC / "工程回归摘要.json"
    if engineering.exists():
        frozen(target / engineering.name, engineering.read_bytes())
    if (target / "工程回归摘要.json").exists():
        value = {**value, "engineering": json.loads((target / "工程回归摘要.json").read_text())}
    (target / "README.md").write_text(
        "# 全局规划绑定修复实测\n\n两组均DeepSeek Flash、关闭思考、2048有效输出上限、单次25秒/累计45秒、一次结构修复。"
        "同一进程复用同一客户端；对照加载与前次实测hash一致的Git修复前两文件快照，候选为当前仓库两文件。"
        "不切Git分支或工作区，不修改.env，不执行业务工具。37场景随机、逐场景AB/BA轮换。\n\n"
        "- cases.jsonl：原7失败＋新增30长尾；输入、期望及scope。\n"
        "- results.jsonl：74条观察的最终JSON、评分、异常、修复次数与全部用量；不保存隐藏推理文本。\n"
        "- manifest.json：模型、预算、数据hash、修复前commit和两组源代码hash。\n"
        "- baseline_source/：受测原始两文件，便于复算错误，不是当前生产实现。\n"
        "- summary.json：7和30两个分母分开；该集定向富集绑定失败，不代表一般规划总体准确率。\n"
        "- original30/（若存在）：修复后另测完整D04，检查原有非绑定场景回归；单臂，不与旧观察拼成新A/B。\n\n"
        "- 工程回归摘要.json（若存在）：不含私有.env的隔离源码副本受控测试；不计入模型准确率。\n\n"
        "数据为人工合成，标签及评分覆盖数量/领域/关键词/依赖/绑定，不是完整自然语言质量、实际写入或端到端完成率。"
        "部分原7与D04重叠，不能把数量相加当独立样本；正常服务没有在此实验中部署或重启。\n")
    return value


def tokens(item):
    return str(item["total_tokens"]) if item["total_tokens"] is not None else f"≥{item['observed_total_tokens']}（{item['attempts_missing_usage']}次未知）"


def section(preparation, measured=None):
    dataset = preparation["dataset"].removeprefix("测试回报/")
    lines = ["## 0.1 关闭思考：全局规划绑定修复", "",
        "按用户要求撤下思考模式对比，保持`MODEL_THINKING=disabled`。原始实验仅保留在历史归档，不再自动加入本报告。", "",
        "### 修复内容", "",
        "- 绑定左侧字段使用显式枚举，来源路径带格式约束；模型看到的Schema与宿主约束对齐。仅暴露跨任务绑定接口，不把所有领域参数重新交给全局规划器。",
        "- 接收字段按领域校验，保存完整查询回执明确使用`details ← t1.result`，路线使用`details ← t1.route`；不把非法别名静默改成合法字段。",
        "- 从包装后的解析异常中提取字段、类型和允许值，写入Trace并提供一次精确修复；不回传原始输入/隐藏推理或任意异常文本，预算和重试次数不增加。",
        "- 提示词明确同域独立目标仍应拆分，例如查询提醒与查询行程；检索地点后导航仍可由一个领域子图完成。", "",
        f"[冻结数据集：原7失败＋30长尾]({dataset})，共{preparation['cases']}场景：天气、路线、检索保存、多任务依赖、否定/纠正边界五组各6条新样本；"
        f"{preparation['binding_positive_cases']}条需要绑定、{preparation['no_write_control_cases']}条禁止保存对照。"
        "旧样本与新长尾分母分别报告，评分允许等价ID及并行顺序，不是37个线上用户。", ""]
    if not measured:
        return "\n".join(lines + ["真实模型测试尚未完成；不填写修复后通过率、时延或Token。工程校验不代替模型效果。", ""])
    lines += ["### 真实修复前后配对", "", "| 范围 | 通过/提交 前→后 | P50秒 前→后 | P95秒 前→后 | Token总量 前→后 |",
              "|---|---|---|---|---|"]
    pair_notes = []
    for scope, name in (("failed_seed7", "原7条失败"), ("long_tail30", "新增30条长尾")):
        a, b = (measured["arms"][arm]["scopes"][scope] for arm in ("baseline", "candidate"))
        lines.append(f"| {name} | {a['passed']}/{a['submitted']} → {b['passed']}/{b['submitted']} | "
            f"{a['p50_ms']/1000:.3f} → {b['p50_ms']/1000:.3f} | {a['p95_ms']/1000:.3f} → {b['p95_ms']/1000:.3f} | {tokens(a)} → {tokens(b)} |")
        pair = measured["paired"][scope]
        pair_notes.append(f"- {scope}配对：救回{len(pair['rescued'])}条；新增退化{len(pair['regressed'])}条。")
    lines += ["", *pair_notes, ""]
    if measured.get("original30"):
        item = measured["original30"]["groups"]["planner"]
        lines += [f"另测原完整30条规划回归：修复后**{item['passed']}/{item['submitted']}**，P50 {item['p50_ms']/1000:.3f}秒、"
            f"P95 {item['p95_ms']/1000:.3f}秒，Token {tokens(item)}。这是单臂补充，不能和旧23/30拼为新配对。", ""]
    if measured.get("engineering"):
        item = measured["engineering"]
        lines += [f"工程回归：不带私有.env/API凭证的隔离源码副本，{item['passed']}通过、{item['failed']}失败、"
                  f"{item['skipped']}跳过。受控回归不计入真实模型样本或会话完成率。", ""]
    lines += [f"[逐条结果、受测源码与运行条件]({PUBLIC.as_posix()}/README.md)。两组同模型、关闭思考，"
        "只改变绑定契约/宿主校验/修复反馈及原子目标提示词；不是更换模型，也未部署到正常服务。", "",
        "新长尾中有2条首轮把标题字面值放进bind_slots.title，被来源路径校验拒绝；一次精确反馈后把标题留在任务query中、只绑定真实回执，随后通过。"
        "这两次失败尝试及Token没有剔除；说明不仅要防非法字段，也要防把静态槽位值误当执行证据路径。", "",
        "**范围限制：** 本集专门富集此前绑定缺陷，不能把该集修复前低通过率称为整个系统准确率，也不能由局部规划改善推出端到端完成率。"
        "这是一次规划回归，不是未见生产流量泛化；后续完整会话复验单独列出，不回写本节配对成绩。", ""]
    return "\n".join(lines)
