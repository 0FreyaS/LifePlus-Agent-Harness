"""Build the DeepSeek-only test handbook and annotated datasets without API calls."""
from __future__ import annotations

import argparse
import hashlib
import json
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from .jev_ab import percentile, summarize, usage_summary

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "测试回报"
DATE = "2026-10-30"
EVIDENCE = Path("evaluations/jev/2026-10-29-deepseek/v1")
REDIRECT = "<!-- lifeplus-test-report:redirect -->"


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def write_frozen(path, data):
    if path.exists() and path.read_bytes() != data:
        raise ValueError("Frozen evidence differs; use a new version: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def json_lines(rows):
    return "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode()


# Each companion note is part of the dataset contract, not a JSONL comment.
SPECS = [
    ("D01", "01-领域划分", "领域仲裁21条", 21, "domain", "已有DeepSeek局部配对",
        EVIDENCE / "datasets/domain.jsonl", None,
        "七领域各3条（含上下文表达）；标注expected_domain、memory_brief。测领域分类，不测工具选择或槽位。未覆盖完整拒判/域外分布。"),
    ("D02", "01-领域划分", "单任务域与动作30条", 30, "single_task", "待DeepSeek专项复测",
        Path("evaluations/data/lifeplus-v1.jsonl"), "single_task",
        "标注expected.domain/action；分别算领域准确率、领域与动作联合正确率。不能把联合正确率称为领域准确率。工具专用正确标签仍需独立补标。"),
    ("D03", "02-槽位与参数", "槽位提取30条", 30, "slots", "待DeepSeek专项复测",
        Path("evaluations/data/lifeplus-v1.jsonl"), "slot_extraction",
        "30条、44个已标注字段，含3个None约束。标注expected.domain/slots；None表示不得臆造。比较城市、日期、起终点、模式、歌手、商家筛选和提醒时间；只算已标注字段，不称全槽位F1。"),
    ("D04", "03-规划与依赖", "规划与依赖30条", 30, "planner", "待DeepSeek完整规划专项复测",
        Path("evaluations/planner_cases.py"), None,
        "来源为planner_cases.CASES；任务语义label不要求与模型ID相同。固定北京时间2026-10-27T10:00:00+08:00解释日期；全局GoalPlan只检查目标、领域、依赖与绑定，工具/参数另在领域层验收。"),
    ("D05", "04-信息澄清", "缺参与完整信息30条", 30, "clarification", "待DeepSeek正负例专项复测",
        Path("evaluations/data/lifeplus-v1.jsonl"), "clarification",
        "15个必要澄清正例、15个完整信息负例；expected.missing_field为期望字段或None。旧正例按未提供默认起点/定位的实验条件标注；重跑需固定配置，不能将生产默认地点与实时GPS混同。"),
    ("D06", "05-端到端任务", "完整任务90会话", 90, "completion", "待DeepSeek当前整系统完整复测",
        Path("evaluations/data/completion-v1.jsonl"), None,
        "单任务、多任务、澄清后完成各30会话。goals定义独立用户目标，user_policy是预先约定的回复策略；依据工具参数、真实回执、确认顺序和写入证据验收，不只看completed。"),
    ("D07", "05-端到端任务", "无标点复合回复6会话", 6, "completion", "待DeepSeek专项复测",
        Path("evaluations/data/completion-compound-v1.jsonl"), None,
        "验证没有逗号时恢复旧任务并创建新任务，独立于90会话统计。不能因识别到连接词就漏掉原回复或擅自执行否定需求。"),
    ("D08", "06-文本事件", "文本事件36条", 36, "events", "已有DeepSeek局部配对",
        EVIDENCE / "datasets/events.jsonl", None,
        "纯回复12、恢复加新任务12、边界12。expected包含route、task_id、候选索引及原文片段，expected_filter为Jev二分类标签。局部校区候选是人工快照，不是实时地图回执。"),
    ("D09", "05-端到端任务", "领域开关全链路12会话", 12, "completion", "已有DeepSeek领域开关配对",
        EVIDENCE / "datasets/domain-e2e.jsonl", None,
        "仅切换领域llm/jev_cascade；事件为当时的llm规则优先、数量门控rules。5单任务、2多任务、2澄清、2复合事件、1四目标。不是当前llm_direct策略下的12例验收。"),
    ("D10", "06-文本事件", "文本与卡片5会话", 5, "completion", "已有DeepSeek配对及当前策略单臂复测",
        EVIDENCE / "datasets/events-e2e.jsonl", None,
        "文本选歌曲、补城市、两种恢复加新需求、结构化卡片对照。自由文本从composer提交，不借卡片焦点。A/B与切换后复测复用这5条，不能当成15个独立场景。"),
    ("D11", "05-端到端任务", "基础查询重复8次", 8, "completion", "已有DeepSeek单组结果",
        Path("evaluations/model_comparison/2026-10-29-deepseek-flash/v1/cases.jsonl"), None,
        "天气和歌手介绍各重复3次，多任务及澄清各1次，只有4类Query。只纳入DeepSeek观察，不再展示其他模型对比；当时事件策略为llm，不是当前整系统SLA。"),
    ("D12", "07-候选选择", "候选选择30条", 30, "controlled", "有历史模型无关组件证据",
        Path("evaluations/data/lifeplus-v1.jsonl"), "candidate_selection",
        "导航、音乐、本地生活各10种选择；固定候选，验证索引、口语序数、非法选择、零/单候选及POI绑定，不调用真实理解模型。"),
    ("D13", "08-恢复与幂等", "恢复与写操作30条", 30, "controlled", "有历史模型无关组件证据",
        Path("evaluations/data/lifeplus-v1.jsonl"), "recovery",
        "三种写动作各10场景：确认、拒绝、未确认、错误凭证、重开检查点、取消、去重、冲突、迟到回执、线程隔离。图和队列真实，写回执受控，不是远端恰好一次保证。"),
    ("D14", "09-工具容错", "故障注入30条", 30, "controlled", "有历史模型无关组件证据",
        Path("evaluations/data/lifeplus-v1.jsonl"), "tool_failure",
        "六工具各5种故障：永久错误、超时、瞬态重试、依赖失败、并行进度；注入短超时仅为测试加速，不是生产超时配置。"),
    ("D15", "10-真实工具", "真实MCP工具30条", 30, "real_tools", "有历史真实工具证据，待当前复验",
        Path("evaluations/data/lifeplus-v1.jsonl"), "real_tools",
        "天气6、POI6、音乐6、附近商家4、路线4、详情1、行程保存2、读取1。真实MCP/外部接口/隔离本地库，不依赖LLM准确率；写操作仅允许专用评测用户和数据库。"),
    ("D16", "12-RAG时效", "RAG历史删除与缩短2条", 2, "rag_audit", "历史2例失败，待当前索引复验",
        Path("data/testing-audit-20261028/summary.json"), None,
        "历史模型无关Chroma审计记录，包含删除/缩短文档前后chunk数量与残留检索结果。每行保留期望与实际，不是相关性标注集，不能计算Recall@K/MRR。"),
    ("D17", "03-规划与依赖", "规划绑定修复37条-v1", 37, "planner_binding", "已冻结；修复前后实测状态见第0.1节",
        Path("测试回报/测试数据集/03-规划与依赖/规划绑定修复37条-v1.jsonl"), None,
        "原7失败＋新30长尾分开；五类长尾各6条，总计34条需要绑定、3条禁止保存对照。只评目标/领域/关键词/依赖/绑定，非端到端完成率或生产流量。"),
]


METRICS = [
    ("M01", "领域划分准确率", "正确领域数 / 全部提交数，异常计错", "D01、D02", "D01实测；D02待测"),
    ("M02", "领域＋动作联合准确率", "域与动作都正确的样本 / 全部提交样本", "D02", "待DeepSeek专项复测"),
    ("M03", "工具选择正确率", "选对工具的任务 / 需要选工具的已提交任务", "D02补工具标签", "待补独立工具标签并测试"),
    ("M04", "标注字段槽位准确率", "正确标注字段 / 全部标注字段；当前44字段，异常全错", "D03", "待测，不能沿用旧值"),
    ("M05", "整组标注槽位正确率", "该条全部标注槽位正确 / 30条；领域另算", "D03", "待测，不是参数合法率"),
    ("M06", "参数合法率／工具参数联合正确率", "分别验类型与业务约束；联合要求选对工具且全部参数语义正确", "D03＋工具专用标签", "待补标签及专项测试"),
    ("M07", "目标拆分精确率与召回率", "匹配目标/生成目标；匹配目标/标注目标，重复目标处罚", "D04、D06", "待当前完整专项测量"),
    ("M08", "计划及依赖绑定正确率", "目标、等价DAG和绑定均正确的计划 / 全部规划请求", "D04、D17", "有失败诊断，无30条完整测量"),
    ("M09", "必要澄清召回率", "应澄清且实际触发 / 必要澄清正例15条；字段正确另算", "D05", "待正负例复测"),
    ("M10", "多余澄清率", "不需澄清却追问 / 完整输入负例15条；异常另报", "D05", "待正负例复测"),
    ("M11", "澄清后任务完成率", "澄清后所有目标完成的会话 / 全部澄清目标会话", "D06澄清30条", "小样本有成功，不代替30条结果"),
    ("M12", "完整事件理解准确率", "route、归属、索引、全部原文片段正确 / 全部36条", "D08", "已有配对实测"),
    ("M13", "复合回复新需求保留率", "恢复和新增片段完整 / 全部复合回复12条", "D08复合组", "已有局部实测12/12"),
    ("M14", "Jev接管率与不安全捷径率", "纯回复接管/纯回复12条；误走纯恢复/非纯回复24条", "D08", "已有实测，非最终准确率"),
    ("M15", "会话端到端完成率", "全部约定目标完成的会话 / 全部提交会话，部分成功不算全成功", "D06、D09、D10", "小样本实测；90条待测"),
    ("M16", "目标级完成率", "已完成子目标 / 所有已标注子目标，未创建也计失败", "D06、D09、D10", "已有小样本实测"),
    ("M17", "结构化虚假完成率", "状态宣称完成但独立目标验收失败 / 状态宣称完成数", "端到端原始结果", "有契约检查；待完整专测，不等于回复幻觉率"),
    ("M18", "最终完成P50/P95", "提交到全部目标终态的nearest-rank分位数；保留失败与超时", "各端到端数据集", "已有小样本，不作生产SLA"),
    ("M19", "首增量及阶段延迟", "首增量分模型级和请求级；排队、工具、人工等待分开", "Trace＋时间戳", "模型attempt有记录；全请求TTFT待完整测"),
    ("M20", "Token总量及单位完成消耗", "全部attempt的reported total；每提交及每成功目标分别作分母", "各实测结果", "部分实测有未知用量；不能当0"),
    ("M21", "恢复／容错场景通过率", "该场景全部断言满足 / 已提交场景，次数和清理也验收", "D12、D13、D14", "历史组件证据＋当前受控回归"),
    ("M22", "未授权／重复写入／取消后执行", "分别记录事件次数及实际受测写入/取消次数", "D13＋Trace", "有受控门禁；无生产零事件保证"),
    ("M23", "真实工具证据通过率", "非模拟且具备独立最低业务证据 / 实际提交工具场景", "D15", "历史30条；当前全工具复验待做"),
    ("M24", "并行与依赖调度正确性", "在途区间重叠、依赖完成后启动、绑定真实回执分别断言", "D04、D06＋工程用例", "受控回归有证据，独立真实依赖仍有失败"),
    ("M25", "RAG检索与时效", "有相关性标签才算Recall@K/MRR；删除和缩短后无旧chunk另测", "D16时效；相关性标注待补", "历史时效2例失败，当前待复验"),
    ("M26", "记忆及原文检索正确性", "原文精确匹配、偏好更新/删除、跨任务隔离分别验收", "工程用例索引", "当前受控覆盖；真实长会话专项待测"),
    ("M27", "人工Rubric与Judge一致性", "固定维度评分并与人工双标对齐，报告一致率/分歧", "人工评测集待补", "未测，不填写4.2/5等目标值"),
    ("M28", "容量与生产可用性", "固定并发及请求分布测吞吐、错误、P95、多实例恢复", "压测/故障演练待补", "未测，不沿用日均请求或生产QPS"),
]


def bundle(output, spec):
    ident, folder, name, count, kind, status, source, group, note = spec
    directory = output / "测试数据集" / folder
    directory.mkdir(parents=True, exist_ok=True)
    cases_path = directory / (name + ".jsonl")
    if kind == "planner":
        from .planner_cases import CASES
        cases = CASES
    elif (ROOT / source).exists():
        if kind == "rag_audit":
            cases = [{"id": "rag-" + case["case"], **case}
                for case in json.loads((ROOT / source).read_text())["rag_docker"]["cases"]]
        else:
            cases = read_rows(ROOT / source)
        if group:
            cases = [case for case in cases if case["group"] == group]
    elif cases_path.exists() or (ROOT / "测试回报/测试数据集" / folder / (name + ".jsonl")).exists():
        retained = cases_path if cases_path.exists() else ROOT / "测试回报/测试数据集" / folder / (name + ".jsonl")
        cases = read_rows(retained)
    else:
        raise FileNotFoundError("Dataset unavailable, including portable copy: " + str(source))
    if len(cases) != count or len({case["id"] for case in cases}) != count:
        raise ValueError("Dataset count or IDs do not match: " + ident)
    write_frozen(cases_path, json_lines(cases))
    details = {"id": ident, "file": str(cases_path.relative_to(output)), "cases": count,
        "source": str(source), "source_group": group, "format": kind, "measurement_status": status,
        "sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(), "purpose": note}
    if ident == "D03":
        fields = [value for case in cases for value in case["expected"]["slots"].values()]
        details.update(annotated_fields=len(fields), must_not_infer_fields=sum(value is None for value in fields))
    (directory / (name + ".说明.md")).write_text(
        f"# {ident} {name}\n\n- 用途：{note}\n- 规模：{count}条，单位以场景/会话定义为准。\n"
        f"- 当前状态：{status}。\n- 原来源：`{source}`"
        + (f"，筛选`group={group}`" if group else "")
        + "。\n- JSONL保持纯JSON，不在行内插入注释；本文件是对应说明。\n"
        "- 来源为人工编写/合成回归数据，不是生产日志或模型训练集。已查看/复用的样本不称全新盲测。\n"
        "- 保留旧字段和期望；不因输出失败改标签，新增需求另建版本。相对日期按各次manifest的北京时间锚点解析。\n"
        "- 文件之间可能复用Query/目标/模板，不把文件行数相加当独立用户数。\n"
        "- 当前生产固定配置地点、用户历史和默认值必须写入运行条件；None与缺失标签不能随配置变化静默重解释。\n"
        "- 整理命令本身不发起API请求；真实模型/工具实测由专用runner执行，需固定入口、预算及隔离存储。\n")
    return details


def validate_baseline(rows):
    for row in rows:
        if row.get("model") and row["model"] != "deepseek-flash":
            raise ValueError("Non-baseline model observation in current report")
        for attempt in row.get("attempts", []):
            if attempt.get("model_name") not in {None, "deepseek-flash", "jev-1.14.0"}:
                raise ValueError("Non-baseline attempt in current report")


def export_results(output, relative, source, *, arm=None):
    target = output / "测试数据集" / "结果记录" / relative
    origin = ROOT / source
    if origin.exists():
        rows = read_rows(origin)
        if arm:
            rows = [row for row in rows if row.get("arm") == arm]
        validate_baseline(rows)
        write_frozen(target, json_lines(rows))
    elif target.exists() or (ROOT / "测试回报/测试数据集/结果记录" / relative).exists():
        retained = target if target.exists() else ROOT / "测试回报/测试数据集/结果记录" / relative
        rows = read_rows(retained)
        validate_baseline(rows)
        write_frozen(target, json_lines(rows))
    else:
        raise FileNotFoundError("Missing retained result: " + str(source))
    validate_baseline(rows)
    return rows


def summary(rows):
    attempts = [attempt for row in rows for attempt in row.get("attempts", [])]
    values = [row["elapsed_ms"] for row in rows]
    return {"submitted": len(rows), "passed": sum(bool(row.get("passed", row.get("completed"))) for row in rows),
        "p50_ms": percentile(values, .5), "p95_ms": percentile(values, .95),
        "goals_completed": sum(row.get("goals_completed", 0) for row in rows),
        "goals_total": sum(row.get("goals_total", 0) for row in rows), **usage_summary(attempts)}


def token_text(value):
    return str(value["total_tokens"]) if value["total_tokens"] is not None else f"≥{value['observed_total_tokens']}（{value['attempts_missing_usage']}次未知）"


def comparison(label, rows):
    a, b = [summary([row for row in rows if row["arm"] == arm]) for arm in ("baseline", "candidate")]
    return (f"| {label} | {a['passed']}/{a['submitted']} → {b['passed']}/{b['submitted']} | "
        f"{a['p50_ms']/1000:.3f} → {b['p50_ms']/1000:.3f} | {a['p95_ms']/1000:.3f} → {b['p95_ms']/1000:.3f} | "
        f"{token_text(a)} → {token_text(b)} |")


def engineering(output):
    directory = output / "测试数据集" / "11-工程回归"
    directory.mkdir(parents=True, exist_ok=True)
    path = ROOT / "data/llm-direct-snapshot-tests-20261029.xml"
    cases_path = directory / "发布快照用例索引.jsonl"
    if path.exists():
        tree = ET.parse(path)
        cases = [{"id": f"{item.get('classname')}::{item.get('name')}", "module": item.get("classname"),
            "name": item.get("name"), "duration_s": float(item.get("time", 0)),
            "status": "skipped" if item.find("skipped") is not None else "failed" if item.find("failure") is not None
                or item.find("error") is not None else "passed"} for item in tree.iter("testcase")]
        write_frozen(cases_path, json_lines(cases))
    else:
        retained = cases_path if cases_path.exists() else ROOT / "测试回报/测试数据集/11-工程回归/发布快照用例索引.jsonl"
        cases = read_rows(retained)
        write_frozen(cases_path, json_lines(cases))
    counts = Counter(row["status"] for row in cases)
    (directory / "发布快照用例索引.说明.md").write_text(
        "# 工程回归用例索引\n\n来源为2026-10-29待发布隔离快照JUnit，790通过、2跳过。每行是一个参数化pytest用例，包含所属模块、状态和运行时间；不是792个真实用户会话，也不代表模型准确率。\n\n"
        "覆盖事件、槽位Schema、候选/确认、中断恢复、预算、工具故障、并行、SSE、路线、记忆、访问控制和部署等；模型/工具多数受控。真实模型和外部服务的结果另列。原始本机JUnit：`data/llm-direct-snapshot-tests-20261029.xml`，未把私有目录、主机标识或认证信息拷入本索引。\n")
    return {"source": "data/llm-direct-snapshot-tests-20261029.xml", "date": "2026-10-29", "counts": dict(counts),
        "by_module": {name: dict(Counter(row["status"] for row in cases if row["module"] == name))
            for name in sorted({row["module"] for row in cases})}}


def build(output=OUTPUT):
    from .current_measurements import load as load_current
    from .planner_binding_cases import DATASET, PREPARATION, preparation_record
    from .planner_binding_report import load as load_binding

    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    current = load_current(output, ROOT)
    measured_datasets = {"D02", "D03", "D04", "D05", "D06"} if current else set()
    datasets = [bundle(output, (*spec[:5], "2026-10-30当前配置实测，见本轮结果及条件", *spec[6:])
                       if spec[0] in measured_datasets else spec) for spec in SPECS]
    binding_path = output / DATASET.relative_to("测试回报")
    preparation = preparation_record(read_rows(binding_path), binding_path.read_bytes())
    write_frozen(output / PREPARATION.relative_to("测试回报"), json_text(preparation).encode())
    binding = load_binding(output, ROOT)
    results = {}
    for name, relative in (("domain", "modules/domain"), ("events", "modules/events"),
        ("domain_e2e", "end_to_end/domain"), ("events_e2e", "end_to_end/events"), ("current", "rollout/llm-direct")):
        results[name] = export_results(output, name + ".jsonl", EVIDENCE / relative / "results.jsonl")
    results["basic"] = export_results(output, "basic.jsonl",
        Path("evaluations/model_comparison/2026-10-29-deepseek-flash/v1/results.jsonl"), arm="candidate")
    # Model-independent historical components are explicitly not current-model quality evidence.
    component_path = ROOT / "data/testing-audit-20261028/components/results.jsonl"
    cache = output / "测试数据集/结果记录/historical_components.jsonl"
    if component_path.exists():
        results["components"] = [row for row in read_rows(component_path)
            if row["group"] in {"candidate_selection", "recovery", "tool_failure", "real_tools"}]
        write_frozen(cache, json_lines(results["components"]))
    else:
        retained = cache if cache.exists() else ROOT / "测试回报/测试数据集/结果记录/historical_components.jsonl"
        results["components"] = read_rows(retained)
        write_frozen(cache, json_lines(results["components"]))
    regression = engineering(output)
    verification_path = output / "测试数据集/结果记录/报告维护验证-20261030.json"
    verification_source = ROOT / "data/test-report-review-tests-20261030.xml"
    if verification_source.exists():
        verification_cases = list(ET.parse(verification_source).iter("testcase"))
        verification = {"date": DATE, "source": "data/test-report-review-tests-20261030.xml",
            "passed": sum(not any(case.find(kind) is not None for kind in ("skipped", "failure", "error")) for case in verification_cases),
            "skipped": sum(case.find("skipped") is not None for case in verification_cases),
            "failed": sum(case.find("failure") is not None or case.find("error") is not None for case in verification_cases),
            "scope": "2026-10-30工作区隔离副本工程回归，含报告维护及未提交文档工具；不是模型准确率或部署验收"}
        write_frozen(verification_path, json_text(verification).encode())
    else:
        retained = verification_path if verification_path.exists() else ROOT / "测试回报/测试数据集/结果记录/报告维护验证-20261030.json"
        verification = json.loads(retained.read_text()) if retained.exists() else None
        if verification:
            write_frozen(verification_path, json_text(verification).encode())
    extra_path = output / "测试数据集/结果记录/历史工具与容器专项.json"
    if all((ROOT / "data" / name).exists() for name in ("tool-integration-latest.json",
        "local-life-tools-latest.json", "runtime-hardening-tool-smoke-20261024.json", "container-check-20261028.json")):
        extra = []
        for filename, label in (("tool-integration-latest.json", "真实工具集成"),
            ("local-life-tools-latest.json", "本地生活真实工具"),
            ("runtime-hardening-tool-smoke-20261024.json", "容错后真实工具冒烟")):
            source = ROOT / "data" / filename
            value = json.loads(source.read_text())
            extra.append({"name": label, "source": str(source.relative_to(ROOT)), "date": value["tested_at"][:10],
                "passed": value["passed"], "submitted": value["total"],
                "scope": "历史真实MCP工具，无理解模型；不作为当前模型准确率或当前服务可用率",
                "cases": [{"id": case["case"], "passed": case["passed"]} for case in value["cases"]]})
        source = ROOT / "data/container-check-20261028.json"
        value = json.loads(source.read_text())
        extra.append({"name": "隔离容器受控验收", "source": str(source.relative_to(ROOT)), "date": "2026-10-28",
            "passed": sum(value["checks"].values()), "submitted": len(value["checks"]),
            "scope": "历史真实Docker、demo任务；不调用真实模型或业务接口",
            "cases": [{"id": name, "passed": passed} for name, passed in value["checks"].items()]})
        write_frozen(extra_path, json_text(extra).encode())
    else:
        retained = extra_path if extra_path.exists() else ROOT / "测试回报/测试数据集/结果记录/历史工具与容器专项.json"
        extra = json.loads(retained.read_text())
        write_frozen(extra_path, json_text(extra).encode())
    statistics = {name: {arm: summary([row for row in rows if row.get("arm", "current") == arm])
        for arm in sorted({row.get("arm", "current") for row in rows})} for name, rows in results.items()}
    pair_stats = {name: summarize(results[name]) for name in ("domain", "events", "domain_e2e", "events_e2e")}
    known_pairs = {}
    for name in pair_stats:
        rows = results[name]
        identifiers = [identifier for identifier in sorted({row["id"] for row in rows})
            if {row["arm"] for row in rows if row["id"] == identifier} == {"baseline", "candidate"}
            and all(usage_summary(row["attempts"])["attempts_missing_usage"] == 0
                for row in rows if row["id"] == identifier)]
        known_pairs[name] = {"ids": identifiers, "arms": {
            arm: usage_summary([attempt for row in rows if row["id"] in identifiers and row["arm"] == arm
                for attempt in row["attempts"]]) for arm in ("baseline", "candidate")}}
    catalog = [{"id": ident, "name": name, "definition": definition, "dataset": dataset, "status": status}
        for ident, name, definition, dataset, status in METRICS]
    if current:
        for metric in catalog:
            if metric["id"] in {"M01", "M02", "M04", "M05", "M08", "M09", "M10", "M11", "M15", "M16", "M17", "M18", "M20"}:
                metric["status"] = "2026-10-30当前配置实测，见第0节；保留各专项口径"
    for metric in catalog:
        if metric["id"] == "M08":
            metric["status"] = "D04修复前基线；D17修复后配对见第0.1节" if binding else "契约已修复，真实模型测量待完成"
        elif metric["id"] in {"M15", "M16", "M17", "M18"}:
            metric["status"] += "；规划修复后完整端到端待复测"
    result_dir = output / "测试数据集/结果记录"
    (result_dir / "README.md").write_text(
        "# 结果文件用途\n\n- domain.jsonl：D01，DeepSeek领域分类与Jev级联配对。\n- events.jsonl：D08，直接DeepSeek与Jev纯回复过滤配对。\n"
        "- domain_e2e.jsonl：D09，领域开关真实全链路配对；事件为当时规则优先。\n- events_e2e.jsonl：D10，文本解释策略全链路配对。\n"
        "- current.jsonl：D10，当前llm_direct部署决定后的单臂复测，不是新A/B。\n- basic.jsonl：D11，仅保留DeepSeek的8条观察，包含同Query重复。\n"
        "- historical_components.jsonl：D12–D15的2026-10-28组件测试；不调用理解模型，版本旧于当前，不能替代当前模型效果。\n"
        "- 汇总.json：由逐条结果复算的质量、时延、Token、混淆矩阵与工程回归索引。未知usage为null，已知下界另列。\n"
        "\n配对结果与部署复测有样本复用，不相加宣传独立样本量；不包含SQLite、完整会话库、API Key或认证数据。\n")
    evidence = {"as_of": DATE, "baseline_model": "deepseek-flash", "thinking": "disabled",
        "current_policy": {"domain": "jev_cascade", "events": "llm_direct", "planning_gate": "rules"},
        "datasets": datasets, "metrics": catalog, "observations": statistics, "paired": pair_stats,
        "complete_usage_pairs": known_pairs,
        "engineering": regression, "report_maintenance_validation": verification,
        "historical_special_checks": extra, "paid_calls_during_organization": 0}
    if current:
        evidence["current_baseline"] = current
    evidence["planner_binding_preparation"] = preparation
    if binding:
        evidence["planner_binding_fix"] = binding
    (result_dir / "汇总.json").write_text(json_text(evidence))
    (output / "测试数据集/数据集目录.json").write_text(json_text(datasets))
    dataset_lines = ["# 测试数据集目录", "", "每个JSONL都有同名`.说明.md`，写明用途、规模、标签、来源、运行边界与实测状态。JSONL本身不插入注释。", "",
        "| ID | 数据集 | 行数 | 用途/状态 |", "|---|---|---:|---|"]
    for dataset in datasets:
        relative = Path(dataset["file"]).relative_to("测试数据集")
        dataset_lines.append(f"| {dataset['id']} | [{relative.stem}]({relative.as_posix()}) | {dataset['cases']} | {dataset['measurement_status']} |")
    dataset_lines += ["", "[工程用例索引](11-工程回归/发布快照用例索引.jsonl)按pytest参数化用例计数，不混入业务场景样本量。",
        "[结果记录](结果记录/README.md)含当前基准的逐条观察和机器可读汇总。文件之间存在场景、目标和模板重用，不计算相加的独立会话总量。", "",
        "## 标注与数据维护", "",
        "1. 用例至少有稳定ID、输入、必要上下文/候选及期望；任务级数据还要有goals和用户回复策略。",
        "2. 增加source、标注人/复核状态、难度、风险和模板/会话分组；当前人工合成集未进行双人一致性验证，不伪称已完成。",
        "3. 验证集按会话/模板隔离。已调提示词和查看过的场景作为回归集；新增未看留出集单独冻结，不复用回归集宣称泛化。",
        "4. 槽位标注要区分当前输入、历史、配置默认、前置回执与不得推断；现有D03只有值标签，来源精确率尚需补标。",
        "5. 计划标签允许任务ID重命名和独立分支顺序等价，但必须保留全部目标、直接依赖和实际结果绑定；不要按输出字符串全等评分。",
        "6. None是负约束，不等于随意漏标。用户未提供地点时，不得因为配置默认存在就擅自改变评测任务定义。",
        "7. 相对日期和动态候选按运行manifest锚点/快照解释；冻结候选与真实地图返回分开。",
        "8. 新数据另建版本。整理脚本拒绝覆盖已存在但内容改变的冻结数据/结果，不能将失败改期望后报提升。",
        "9. 真实写工具必须使用评测用户与隔离存储；整理脚本不运行外部服务。当前新增真实测试及条件见测试总文档第0节（若已有实测）。"]
    (result_dir / "历史工具与容器专项.说明.md").write_text(
        "# 历史工具与容器专项结果用途\n\n同名JSON保留真实工具集成、本地生活、容错工具及Docker受控验收的日期、用例名称和通过数。均非当前LLM准确率；不跨专项相加为独立场景总数，不拷贝Key、资源ID、会话记录或供应商URL。原始来源按每项source追溯。\n")
    (output / "测试数据集/README.md").write_text("\n".join(dataset_lines) + "\n")
    render(output, evidence, results)
    return evidence


def render(output, evidence, results):
    counts = evidence["engineering"]["counts"]
    lines = ["# LifePlus 测试总文档", "", f"整理日期：{DATE}。基础模型：**deepseek-flash，关闭思考**。本次仅盘点与复算已有证据，**没有发起付费API请求，也没有新测槽位准确率**。", "",
        "## 1. 先看结论", "",
        "| 范围 | 已有证据 | 能说明什么 |", "|---|---|---|",
        "| 领域划分 | DeepSeek与Jev级联均21/21 | 人工局部集持平，不是生产100%准确率 |",
        "| 文本事件 | 直接DeepSeek与Jev过滤均34/36 | 纯回复/复合回复各12/12，边界各10/12；采用直接DeepSeek |",
        "| 当前入口完整会话 | 5/5，7/7子目标 | 当前策略小规模回归通过；不等于90会话或生产完成率 |",
        "| 领域开关全链路 | 两组均10/12，子目标14/20 | 当时事件为规则优先；2个依赖保存计划在工具执行前失败 |",
        "| 槽位提取准确率 | **待测**；已整理30条/44个字段 | 不沿用旧模型结果，不从成功工具调用倒推出槽位准确率 |",
        f"| 发布快照工程回归 | {counts.get('passed',0)}通过、{counts.get('skipped',0)}跳过 | 受控代码行为，不能当模型准确率 |", "",
        "当前生产组合为`DOMAIN_ROUTE_PROVIDER=jev_cascade`、`INPUT_EVENT_PROVIDER=llm_direct`、`PLANNING_GATE_PROVIDER=rules`。领域Jev保留，文本Jev过滤不启用。",
        "本报告不合并不同时期、不同入口策略、重复Query与不同测试层的数据，不用一个‘总通过率’覆盖所有能力。其他模型的历史结果不再进入本期总表；原始记录保留。", "",
        "## 2. 指标清单与统计标准", "", "先定义单位与分母，再运行测试。以下是完整指标目录，不代表每项都已有测量。未测保持空缺，不填简历目标或经验值。", "",
        "| ID | 指标 | 计算/判定标准 | 对应数据 | 状态 |", "|---|---|---|---|---|"]
    lines += [f"| {m['id']} | {m['name']} | {m['definition']} | {m['dataset']} | {m['status']} |" for m in evidence["metrics"]]
    lines += ["", "**口径补充：** 领域正确不等于动作/工具/槽位正确；Schema合法不等于语义正确；工具成功不等于用户全部目标完成。槽位字段正确率、整组正确率、参数合法率、工具参数联合正确率须分别报告。", "",
        "## 3. DeepSeek与Jev配对对比", "", "A/B在同组固定数据、提示词、阈值、预算、工具与模型；只改变注明的入口策略。局部与全局串行、随机场景顺序交替AB/BA，单次小样本。", "",
        "| 实验范围 | 完整通过/提交 A→B | P50秒 A→B | P95秒 A→B | 全部Token A→B |", "|---|---|---|---|---|"]
    lines += [comparison("领域21条：DeepSeek→Jev级联", results["domain"]),
        comparison("领域开关全链路12例", results["domain_e2e"]),
        comparison("文本36条：直接DeepSeek→Jev过滤", results["events"])]
    for group, label in (("pure_reply", "其中纯回复12条"), ("compound", "其中恢复＋新任务12条"), ("boundary", "其中边界12条")):
        lines.append(comparison(label, [row for row in results["events"] if row["group"] == group]))
    lines += [comparison("文本策略全链路5例", results["events_e2e"]), "",
        "### 3.1 可以采用的结论", "",
        "- 领域Jev局部P50降低59.63%、P95降低6.38%；全链路P50降低12.49%、P95降低5.07%，完成率没有提升。不能把局部59.63%当整系统提速。",
        "- 领域Jev并未省Token。局部完整用量交集20组4283→15217；全链路完整交集11组28516→32852（+15.21%）。全体各有一次未知usage，不能报精确全量增幅。",
        "- 文本Jev只接管4/12纯回复；复合/边界0/24误走纯恢复捷径。实际返回标签33次，其中30次与标注一致；接管率、原始分类正确率与完整事件准确率是不同分母。",
        "- 文本Jev让事件P50增加42.72%、完整会话P50增加39.31%，因此采用直接DeepSeek。比较不是零模型规则基线，不宣称所有输入都更快。",
        "- 例子：‘无锡校区吧’1.061→0.395秒，B跳过事件LLM；‘无锡校区吧，顺便查下明天无锡天气’0.807→0.932秒，两组均走LLM并保留新需求。不能只展示第一条例子。", "",
        "### 3.2 这两种全局配置不能混用", "",
        "领域12例仅切领域路由，事件仍为当时的`llm`规则优先；文本5例两组领域均Jev、门控均rules，只切`llm_direct/jev_resume_filter`。当前上线选直接LLM，12例旧入口结果仍是风险证据，不自动变为当前12例验收。", "",
        "## 4. 其他完整任务及部署验证", "", "| 结果集 | 完整通过/提交 | 子目标完成 | P50秒 | P95秒 | Token总量 |", "|---|---|---|---|---|---|"]
    for name, label in (("current", "当前文本直达单臂复测"), ("basic", "DeepSeek基础查询8次观察")):
        s = summary(results[name])
        lines.append(f"| {label} | {s['passed']}/{s['submitted']} | {s['goals_completed']}/{s['goals_total']} | {s['p50_ms']/1000:.3f} | {s['p95_ms']/1000:.3f} | {token_text(s)} |")
    lines += ["", "当前5会话为纯回复2、复合回复2、卡片1，均完成；9次事件DeepSeek、0次事件Jev。单臂复测不与旧配对组成新A/B。卡片对照的初始新请求仍需模型，卡片点击本身不新增文本模型调用。",
        "基础查询8次只有4类Query（天气/歌手各重复3次、多任务/澄清各1次），事件是当时的规则优先。它与其他集合有Query复用，不能相加称独立用户数。",
        "本机真实认证HTTP/SSE天气和歌手介绍2/2完成；这只是部署冒烟，不纳入P50/P95，也不是浏览器视觉或并发负载测试。Docker复用已有依赖镜像完成切换，全新构建曾遇依赖下载超时，不能宣称空白服务器构建已经验收。", "",
        *Path(__file__).with_name("bad-case-review.md").read_text().splitlines(), "",
        "## 6. 工程与模型无关测试", "", f"当前发布快照：**{counts.get('passed',0)}通过、{counts.get('skipped',0)}跳过**；不是模型准确率。该快照为2026-10-29已完成测试，本次整理没有伪造新一轮执行。", "",
        "| 历史组件（2026-10-28） | 场景通过 | 模型/工具条件 |", "|---|---|---|"]
    for group, label in (("candidate_selection", "候选选择"), ("recovery", "确认/恢复/幂等"), ("tool_failure", "工具故障"), ("real_tools", "真实MCP工具")):
        selected = [row for row in results["components"] if row["group"] == group]
        lines.append(f"| {label} | {sum(row['passed'] for row in selected)}/{len(selected)} | "
            + ("真实工具、无理解模型；不是当前供应商可用率" if group == "real_tools" else "受控候选/回执/故障，实际图与队列") + " |")
    if evidence["report_maintenance_validation"]:
        verification = evidence["report_maintenance_validation"]
        lines += ["", f"本次报告维护工作区另做隔离回归：**{verification['passed']}通过、{verification['skipped']}跳过、{verification['failed']}失败**（2026-10-30，包含未提交的文档/归档工具）。这不是原发布快照790条结果的追加业务样本，也不是新的模型或生产验收。", ""]
    lines += ["", "这些组件测试版本早于当前，不能替代本次模型指标或当前真实工具复验。零非法写入等断言只针对受测场景，不推出生产永久为零。", "",
        "### 历史其他专项（单独保留，不与当前结果合计）", "",
        "| 专项 | 日期 | 通过/提交 | 条件 |", "|---|---|---|---|"]
    for item in evidence["historical_special_checks"]:
        lines.append(f"| {item['name']} | {item['date']} | {item['passed']}/{item['submitted']} | {item['scope']} |")
    lines += ["", "### 当前快照的模块覆盖", "", "| pytest模块 | 通过 | 跳过 |", "|---|---:|---:|"]
    for module, count in evidence["engineering"]["by_module"].items():
        lines.append(f"| {module} | {count.get('passed',0)} | {count.get('skipped',0)} |")
    lines += ["", "历史独立RAG审计曾发现删除/缩短文档后旧chunk残留，2/2例失败（非模型问题）；本次未复验当前索引版本，作为待关闭风险，不冒充当前检索准确率。记忆、SSE、并行、安全和部署目前主要由上述工程用例覆盖，真实长会话及跨实例故障效果仍待专项验证。", "",
        "## 7. 数据与复算入口", "", "[数据集目录与每文件说明](测试数据集/README.md) · [机器可读指标、结果和混淆矩阵](测试数据集/结果记录/汇总.json)", "",
        "```text", "测试回报/", "├── 测试总文档.md", "└── 测试数据集/", "    ├── README.md / 数据集目录.json",
        "    ├── 01-领域划分/ ... 10-真实工具/", "    │   ├── 具体测试集.jsonl", "    │   └── 同名测试集.说明.md",
        "    ├── 11-工程回归/", "    ├── 12-RAG时效/", "    └── 结果记录/", "```", "",
        "离线整理与复算（不请求任何模型/工具）：", "", "```bash", ".venv/bin/python -m evaluations.test_report", "```", "",
        "冻结JSONL/结果内容变化时脚本拒绝覆盖，新增结果必须建立新版本；原始模型/工具数据库与私有Key不复制。主报告只保留一个汇总入口，旧测试文档作为跳转入口，原完整记录另外归档，避免丢失坏例。", "",
        "## 8. 下一轮测试优先级", "", "1. P0：修复并单独复验两个非法绑定计划；再用当前llm_direct入口跑D06完整90会话，不把修复前后混算。",
        "2. P0：用D03测字段44项、整组30条、不得推断3项，补槽位来源标签；用D02测领域/动作，另补工具正确标签。",
        "3. P1：用D05固定默认定位策略，测15正/15负的澄清；扩大复合事件和否定/引用边界。",
        "4. P1：在当前模型角色、thinking参数、重试和总预算下重测领域工具选择与多任务规划。旧evaluations.run直接创建ChatOpenAI并启用SDK重试，不经过统一模型角色且未传thinking开关；不可直接把旧命令的数字当当前基线，需先对齐评测客户端。",
        "5. P1：复验真实工具、RAG删除/缩短、长记忆和重启；P2再做真实并发、多实例、人工Rubric/Judge对齐与成本账单。", "",
        "## 9. 时延、费用与证据边界", "",
        "- P50/P95按nearest-rank，失败与超时保留；不同阶段分位数不能相加，并行attempt时长合计不是关键路径。失败很快也不能算优化，需同时展示完成率。",
        "- 请求级首响应、模型级首delta、最终完成、人工等待和恢复后的耗时分别记录；当前ASGI实验不包括浏览器/TCP展示，自动用户回复不代表真实思考时间。",
        "- Token优先供应商total_tokens；只有输入/输出时按报告口径相加，推理不重复加。未知usage不计0；完整交集仅为补充，不替代全样本。",
        "- 每提交请求Token与每成功目标Token的分母不同；失败消耗也进入成本。供应商分词不同，Token数不直接等于人民币费用。",
        "- 任务completed、工具ok、写入成功、全部用户目标完成是四层不同依据；当前没有自然语言事实性Rubric和完整虚假完成专项结果。",
        "- 手工回归集存在模板和跨集合复用，没有训练/双标/生产流量证据；小样本100%不能推广到线上。未测指标保持待测，不引用简历验收目标当实测。"]
    text = "\n".join(lines) + "\n"
    if evidence.get("current_baseline"):
        from .current_measurements import section

        text = text.replace(
            "本次仅盘点与复算已有证据，**没有发起付费API请求，也没有新测槽位准确率**。",
            "已追加本轮当前配置真实模型与MCP测试；此前A/B保留，未与本轮混算。")
        text = text.replace("## 1. 先看结论", section(evidence["current_baseline"]) + "\n## 1. 此前证据与选型结论", 1)
        text = text.replace("| 槽位提取准确率 | **待测**；已整理30条/44个字段 | 不沿用旧模型结果，不从成功工具调用倒推出槽位准确率 |",
            "| 槽位提取准确率 | 本轮30条/44字段已实测，见第0节 | 独立字段标签核对，不从工具成功倒推 |")
        text = text.replace("## 8. 下一轮测试优先级", "## 8. 后续优化与尚未测量项")
        start = text.index("1. P0：修复并单独复验")
        end = text.index("## 9.", start)
        text = text[:start] + (
            "1. P0：按本轮失败Trace定位非法依赖绑定、遗漏目标及参数错误；修复须建立新版本、固定全量回归，不覆盖本轮失败。\n"
            "2. D02/D03/D04/D05/D06本轮已执行；仍需补工具正确标签、参数合法性/来源标签、独立不得推断统计，不能只报整组通过数。\n"
            "3. 扩展未见长尾、否定/引用复合事件，双人复核；真实工具、RAG时效、长记忆与跨实例重启另测。\n"
            "4. 生产并发、浏览器首响应、人工Rubric/Judge对齐与账单成本尚未测量；当前单并发回归不替代这些指标。\n\n"
        ) + text[end:]
    from .planner_binding_report import section as binding_section

    text = text.replace("## 0. 本轮当前配置真实测试", "## 0. 修复前实测基线（保留）", 1)
    text = text.replace("### 从证据得到的优化优先级（尚未实施）", "### 修复前诊断（契约与反馈现已修改，见第0.1节）", 1)
    if evidence.get("planner_binding_fix"):
        text = text.replace(
            "1. P0：按本轮失败Trace定位非法依赖绑定、遗漏目标及参数错误；修复须建立新版本、固定全量回归，不覆盖本轮失败。",
            "1. P0：规划绑定与原子目标提示已完成局部修复和实测，见第0.1节；仍需当前配置D06完整90会话复验实际绑定、工具写入和遗漏目标，不覆盖修复前失败。")
    marker = "## 1. 此前证据与选型结论" if evidence.get("current_baseline") else "## 1. 先看结论"
    text = text.replace(marker, binding_section(evidence["planner_binding_preparation"], evidence.get("planner_binding_fix")) + "\n" + marker, 1)
    (output / "测试总文档.md").write_text(text)


def migrate_entry(output):
    old = ROOT / "docs/jev-front-ab-2026-10-29.md"
    if old.exists() and REDIRECT not in old.read_text():
        archive = ROOT / "docs/archive/jev-front-ab-2026-10-29-history.md"
        write_frozen(archive, old.read_bytes())
    relative = "../" + output.relative_to(ROOT).as_posix() if output.is_relative_to(ROOT) else str(output)
    old.write_text(REDIRECT + "\n\n# LifePlus测试记录入口\n\n"
        "当前测试统一以DeepSeek Flash关闭思考为基准，领域、槽位、规划、澄清、文本事件、完整任务、工具与工程回归集中维护。\n\n"
        f"- [测试总文档]({relative}/测试总文档.md)\n- [测试数据集及逐文件说明]({relative}/测试数据集/README.md)\n\n"
        "实测、历史模型无关证据、验收目标与待测项分开，不把旧模型效果迁移为当前指标。原完整记录已归档，原始数据不删除。\n\n"
        "维护命令：`.venv/bin/python -m evaluations.test_report`（离线整理，不请求付费API）。\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--migrate-entry", action="store_true", help="Archive the old report and replace it with the new entry")
    args = parser.parse_args()
    evidence = build(args.output)
    if args.migrate_entry:
        migrate_entry(args.output.resolve())
    print(json.dumps({"output": str(args.output), "datasets": len(evidence["datasets"]),
        "metrics": len(evidence["metrics"]), "paid_calls": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
