# LifePlus 评测集 v1

## 当前唯一汇总入口（2026-10-30）

[测试总文档](../测试回报/测试总文档.md) · [测试数据集及逐文件说明](../测试回报/测试数据集/README.md)。以DeepSeek Flash关闭思考为基准，领域、槽位、工具、规划、澄清、完整任务、容错与工程回归统一列出；实测与待测分开。下文保留旧评测方法和复现背景，不将旧版本成绩迁移为当前模型指标。

测试总文档第5节统一维护Bad Case复盘：按现场、最早偏离点、证据、主因/次生症状、已实施修复、替代方案和验证状态整理12类问题。编辑源为`evaluations/bad-case-review.md`，离线重建时合入总文档；不要只修改生成结果。局部通过、端到端冻结集通过、未修复及测试口径差异分别标识，原始失败不覆盖。

## 后续完整会话与长尾复验

总文档第0.2节保留上轮绑定修复后的90会话、原8失败及本轮针对性修复前的长尾审计：17条真实模型组件与13条受控场景，并单列SSE/重启和真实Chroma生命周期。原8包含于90；该轮17条模型观察只请求一次，修正审计夹具后的复跑不算新模型试验。该轮审计未修改业务实现。第0.3节另列当前事件语义、绑定类型/时效和索引清理的修改及复测，不覆盖旧失败；正常服务未重启或部署。

```bash
python -m evaluations.adversarial_cases
# 以下显式请求真实模型，目录必须全新
python -m evaluations.adversarial_eval --run-live --output evaluations/runs/robustness-NEW
# 仅复跑受控断言：验证输入和运行源码hash后复用旧模型观察，不新增模型费用
python -m evaluations.adversarial_eval --reuse-live evaluations/runs/robustness-NEW --output evaluations/runs/contracts-NEW
# 当前工作区源码在现有容器独立进程、临时目录审计，不替换服务源码或正常索引
python -m evaluations.rag_lifecycle_audit --container <existing-container> --source-current --output evaluations/runs/rag-NEW.json
# 冻结36条自由文本事件回归，显式请求真实模型
python -m evaluations.event_regression --run-live --output evaluations/runs/events-NEW
python -m evaluations.test_report
```

`evaluations/adversarial_contract_checks.py`为显式审计，不包含在常规testpaths中。修复前的过期依赖失败、真实Chroma删除/缩短失败均保留；当前受控场景13/13、真实Chroma生命周期2/2通过。宿主缺Chroma记为阻塞，不通过skip产品缺陷伪装修复。当前真实模型长尾为事件5/5、规划11/11、领域1/1，另有旧事件36/36及完整90会话90/90；这些分母不可混为一个准确率。详见第0.3节及第5节bad case复盘。

离线维护：`.venv/bin/python -m evaluations.test_report`。旧Jev报告入口已跳转；历史完整文档与原始结果保留，不发起付费API请求。

## 当前配置真实复测

`deepseek_baseline`复用统一模型客户端（thinking disabled、SDK retries=0），不读取标准领域作为输入。领域/动作、槽位、澄清和全局规划各30条；澄清工具回执受控、真实模型及图不受控。规划强制进入模型以隔离数量门控。以下命令会产生真实API费用，输出目录必须全新：

```bash
.venv/bin/python -m evaluations.deepseek_baseline --output evaluations/runs/new-components
env DEFAULT_CURRENT_LOCATION= .venv/bin/python -m evaluations.completion --dataset '测试回报/测试数据集/05-端到端任务/完整任务90会话.jsonl' --output evaluations/runs/new-completion --concurrency 1 --case-budget 210
```

冻结澄清标签假设无默认位置，所以上述完整会话命令只在评测进程禁用配置位置；真实模型、MCP、工具回执及隔离存储不替换。若不传该环境变量，测的是当前默认定位与旧标签的兼容性，不能将差异全归因模型。该基准测试当时未修改源码；后续针对性修复见第0.3节，所有失败、修复尝试和未知用量保留。归档结果位于[基准结果目录](../测试回报/测试数据集/结果记录/2026-10-30-01/README.md)，测试总文档第0节列明实测及剩余边界。

## Jev统一测试记录与数据目录

新增[纯旧任务回复过滤实验](jev/2026-10-29-resume-filter/README.md)：`llm_direct`对自由文本直接使用原事件LLM；`jev_resume_filter`只允许高置信度、宿主可完整唯一绑定的纯回复跳过LLM，复合需求仍由LLM拆分。36条冻结局部场景与5组真实全链路配对独立归档，结构化卡片不经过事件模型；后续已批准本机使用`llm_direct`，过滤候选仍不启用。

## 关闭思考的规划绑定修复

[修复前后逐条证据](../测试回报/测试数据集/结果记录/2026-10-30-planner-binding-fix-01/README.md)与测试总文档第0.1节记录真实配对。原7失败0/7→7/7，新长尾3/30→30/30，原完整D04单臂30/30。这里只验目标、领域、依赖和结果绑定；不是工具执行或端到端完成率。

```bash
# 只准备冻结37条，不调用模型；已有文件内容不一致则拒绝覆盖
python -m evaluations.planner_binding_cases
# 显式付费调用；目录需全新，保持MODEL_THINKING=disabled
python -m evaluations.planner_binding_ab --run-live --output evaluations/runs/planner-binding-fix-ab-NEW
# 单臂补充测试（不会自动推送或部署）
python -m evaluations.planner_binding_eval --run-live --output evaluations/runs/planner-binding-single-NEW
python -m evaluations.test_report
```

配对脚本读取与修复前基准hash一致的Git源码快照到独立模块，不切换工作区；两组共享同模型、2048有效输出上限、25秒单次/45秒累计预算及一次结构修复。包含全部失败attempt和用量，7条与30条分开报告；37条专门富集绑定缺陷，不能当一般规划总体准确率。此前思考模式试验已按要求撤出当前总文档，原始记录只在历史归档保留。

后续Jev实验统一维护[测试主文档](../docs/jev-front-ab-2026-10-29.md)。[数据归档](jev/2026-10-29/README.md)集中保存各模块及整体的冻结输入/期望、逐条A/B结果、CSV、准确率/完成率、P50/P95、Token及变化量。领域与联合前序全链路指标分开，未知用量不能当作0。

只整理已有证据、不产生模型调用：

```bash
.venv/bin/python -m evaluations.jev_archive
.venv/bin/python -m evaluations.jev_front_report
```

报告生成会保留文末人工维护区。新数据/新配置建立新日期或版本，不覆盖旧Query和结果；原始SQLite、日志和Trace继续留在`evaluations/runs`。当前已启用领域Jev，事件审核和数量门控仍关闭；下文旧试验的“默认未启用”仅描述当时状态。

## 2026-10-29 当前架构与 Jev 试验

新增三模块实测：`jev_gate_ab`比较零模型数量粗筛与Jev；`jev_event_ab --strategy jev_gate --front-holdout`验证事件明确性审核；`jev_global_ab --candidate front`验证合并数量/领域判定和事件审核的全局效果。`jev_front_report`从保留原始记录重建[三模块报告](../docs/jev-front-ab-2026-10-29.md)。默认不启用；普通12例与针对性4例分开，不混成总体收益。报告列明429、超时、未知用量和并行试验干扰。

当前全局模型输出不含 action/slots 的 `GoalPlan`，工具选择和槽位提取在领域 `understand`。下文 v1 及旧规划报告保留历史条件，不自动视为新版基线。

Jev 的真实服务试验入口已准备但默认关闭：`.venv/bin/python -m evaluations.jev_ab` 只显示准备状态。填入 `JEV_API_KEY` 后显式添加 `--run-live --output <新目录>`，或通过 `--compare-e2e A目录 B目录 --output <新目录>` 对比现有完成率测试的两组记录。未知用量记 null，失败和回退计入总消耗，父子事件按 span_id 去重，不自动启用候选。完整步骤与边界见 [Jev 接入与 A/B 方案](../docs/jev-integration-and-ab.md)。

已增加事件候选选择的局部配对 `evaluations.jev_event_ab`、真实全链路交替AB/BA `evaluations.jev_global_ab` 和可复算报告生成器 `evaluations.jev_report`。参见 [2026-10-29真实结果](../docs/jev-ab-2026-10-29.md)。`DOMAIN_ROUTE_PROVIDER` 与 `INPUT_EVENT_PROVIDER` 独立配置；默认均为llm。总Token优先采用供应商total_tokens，包含推理但不重复加，流式回复通过stream_usage采集。

## 规划提速配对 A/B

新增 `planner_cases.py` 的 30 个冻结场景，覆盖单任务标点、独立并行、结果依赖、缺参、上下文和六任务边界。只调用真实规划模型，不执行写工具，也不自动更改默认服务配置。

```bash
.venv/bin/python -m evaluations.planner_ab --output data/benchmarks/my-planner-ab --cases 30 --repeats 2 --concurrency 1
```

同模型、同上下文、固定北京时间，随机场景顺序并交替 AB/BA；B 精简 Schema 元数据并独立限制规划输出，仍经过同一个 `TaskPlan` 和槽位校验。默认保留 `PLANNER_VARIANT=baseline`。连接故障、质量回退、提速不显著或 Token 增加均不能支持启用。详见生成的 `report.md`、逐条 `results.jsonl` 与预先写入的 `manifest.json`，不要用连接快速失败的中位耗时宣称优化成功。

本轮完整结果见 [2026-10-27 规划提速 A/B 结论](../docs/planner-ab-2026-10-27.md)：候选未满足门槛，未启用。

设计、故障测试、指标口径和修复证据已整合到 [18 问与测试验收手册](../docs/LifePlus-Agent-Harness-18问与测试验收手册.md)。本文保留 v1 数据及 CLI 的复跑方式，历史报告不覆盖。

本轮已完成首测、问题修复和完整复测，汇总见 [2026-10-23 实测结论](../docs/evaluation-2026-10-23.md)。首轮失败与复测逐条输出均保留在 `runs/`，不要只看最终通过数量。

## 任务完成率优先

新增 [真实任务完成率方案与可靠性补强](../docs/task-completion.md)：单任务、多任务、澄清后完成各 30 条，模型、MCP 与业务存储均实际运行；根据独立用户目标和回执核验，不再仅看节点或状态是否 completed。

```bash
.venv/bin/python -m evaluations.completion_dataset
.venv/bin/python -m evaluations.completion --output evaluations/runs/my-completion-run --concurrency 3
```

90 条端到端数据与下述 240 条分层数据独立报告，不将两种通过率混合计算。新增的 6 条无标点复合回复专项也单独统计。

## 目的与边界

建立可以复跑、能够逐条定位失败原因的首版基线。数据为本仓库人工设计的合成场景，不来自历史生产日志，也没有对应模型训练记录。每类 30 条，8 类共 240 条。各类独立统计，存在少量跨类复用 Query；受控场景包含参数化变体，不能把总数当作 240 个独立同分布用户会话。

不将此前简历中的目标数值当作实测结果。本轮不修改真实服务 Key，不向正常用户数据库写测试行程，不调用预订、付款或真实设备控制。

## 数据分层

| 场景 | 数量 | 实际经过的链路 | 检查内容 |
|---|---:|---|---|
| single_task | 30 | 真实模型 → 生产 `resolve_intent` | 七个领域、标准动作名称 |
| multi_task | 30 | 生产拆分触发器 → 真实模型 → `TaskPlan` | 20 个独立组合、5 个有依赖组合、5 个不应过度拆分的对照 |
| slot_extraction | 30 | 真实模型 → 生产槽位规范化 | 城市、日期、路线端点、出行方式、歌手、类别、距离、预算、评分、提醒时间；禁止猜定位/ID |
| clarification | 30 | 真实模型 → 实际父图/领域子图/Checkpoint；固定工具回执 | 15 个缺参正例和 15 个完整信息负例，核对中断字段 |
| candidate_selection | 30 | 实际图与 Checkpoint；固定候选 | 导航/音乐/本地生活各 10 种，合法索引、口语序数、非法选择、零/单候选、详情 ID 绑定 |
| recovery | 30 | 实际图、SQLite Checkpoint、Tool Queue；固定写回执 | 三种写动作各 10 种：确认/拒绝/未确认/错误凭证/重开检查点/运行中取消/幂等/冲突/迟到结果/线程隔离 |
| tool_failure | 30 | 实际 Tool Queue；注入故障 | 六种工具各 5 种：永久失败、超时、瞬态重试、依赖失败、独立快分支先完成 |
| real_tools | 30 | 实际 MCP stdio → 高德/音乐/隔离本地数据库 | 天气 6、POI 6、音乐 6、附近商家 4、路线 4、详情 1、行程写入 2、读取 1 |

`multi_task` 主要评测理解与规划，不是 30 个真实外部工具全链路多任务。执行并发、依赖调度、复合恢复与 SSE 还由 `tests/test_multi_task_api.py`、`tests/test_runtime_components.py` 和已有 HTTP smoke 覆盖；回归通过数量不算模型准确率。

固定工具回执明确用于隔离编排变量，不代表这些数据来自高德或真实写入，也不会让 live 请求降级为模拟工具。

## 生成与执行

在项目根目录执行，读取现有 `.env` 的真实模型与高德配置：

```bash
.venv/bin/python -m evaluations.dataset
.venv/bin/python -m evaluations.run --groups all --concurrency 3 --output evaluations/runs/my-run

# 只运行可控流程，不使用外部模型或地图服务
.venv/bin/python -m evaluations.run --groups candidate_selection,recovery,tool_failure --output evaluations/runs/my-controlled

# 只请求真实外部工具
.venv/bin/python -m evaluations.run --groups real_tools --output evaluations/runs/my-tools

# 同日中断后续跑：数据、代码、模型、并发和分组必须一致
.venv/bin/python -m evaluations.run --groups all --concurrency 3 --output evaluations/runs/my-run --resume

.venv/bin/python -m pytest -q --junitxml=data/evaluation-regression-tests.xml
```

评测失败退出码为 1，报告仍保留。`--resume` 不重测已有结果；若想复测失败项或修改了实现，使用新的输出目录，保留旧结果。相对日期以运行当天北京时间解释，跨日不可续跑。真实工具写入的隔离数据库运行后清理；续跑时只重建已完成评测行程写入作为读取前置条件。

## 输出文件

- `data/lifeplus-v1.jsonl`：冻结的数据快照，单行包含 ID、场景、Query、期望、来源与运行方式。
- `runs/<run>/manifest.json`：模型、并发、时间、数据及关键源文件 SHA256，用于识别同一实验版本，不含密钥。
- `results.jsonl`：逐条期望、实际结构化输出、断言结果、错误、耗时及供应商返回的 Token 使用。立即落盘，便于 bad case 复盘。
- `summary.json`：分组计数、分项指标的分子分母、P50/P95、失败 ID。
- `report.md`：可直接阅读的汇总报告与测量边界。

## 指标怎么理解

1. **单任务**：领域准确率与领域+动作联合准确率分开。天气领域正确但动作写成 `get_weather` 不满足本系统标准契约 `weather_lookup`。唯一动作领域由宿主规范化；多动作领域仍需区分实际业务动作。
2. **多任务**：按领域多重集合做精确匹配，重复任务也算错误；统计任务级 precision/recall。独立任务不可乱加依赖；必须依赖的任务检查依赖边和输出绑定。不代替动作与槽位联合正确率。
3. **槽位**：`正确标注字段数/标注字段总数`，以及 `全部标注字段正确的样本数/30`。只对标注字段评分，并单独检查领域；未标注字段不参与，不能写成全槽位 precision/recall/F1。城市允许“市”后缀差异；时间按 ISO 时间点、距离按米比较，缺失和臆造都算错。
4. **澄清**：缺参正例用 `触发澄清数/15`，并另算字段正确召回；完整信息对照用 `多余澄清数/15`；总体中断字段准确率分母为 30。异常/超时计为样本失败，必要澄清召回计漏召；完整输入遇到异常时还需结合 runtime error 看，不能当作正确“不澄清”。本轮未测用户补充后的自然语言完成率。
5. **选择/恢复/故障**：每条包含多个断言，全部满足才通过。零非法写入仅表示本次受测确认场景未出现，不保证生产环境永远为零。取消运行中写入不意味着远端撤销，迟到成功必须保留审计证据。
6. **真实工具**：同时检查 `ok`、非模拟、最低限度业务证据。天气需有来源与天气字段；POI 需有 ID/坐标；路线需有距离与时间；音乐需有 URL。业务上合理的空结果不代表服务失败，但此版本选用预期有结果的稳定检索问题，因此空结果会计为该样本失败并保留回执分析。
7. **耗时**：nearest-rank P50/P95，包含失败和超时。真实模型理解、工具调用、受控恢复的耗时不能混为端到端时延。v1 历史报告当时采用 25 秒 timeout、1 次 SDK retry，评测外层 75 秒保护；当前实现关闭 SDK 隐式重试，格式修复最多额外一次且计入预算，不把两种重试混称。故障注入的 80ms timeout 仅为加速测试。
8. **成本**：记录实际 Token usage，不编造未提供的供应商用量，不估算未经核验的货币价格。成功请求 Token 总量不能代替每成功业务任务成本。

30 条样本用于找问题，不足以证明生产 SLA；30/30 对独立同分布假设下的 Wilson 95% 区间约为 88.6%–100%，且本数据存在模板相关性。自然回复的事实性与亲切程度未完成人工 Rubric/独立 judge 验证，不生成对应“平均分”。

## 数据迭代规范

保持 v1 期望不随失败输出修改。新需求、线上失败改为脱敏后新增 v2，并按会话/模板分组去重。区分路由错误、槽位遗漏/臆造、依赖错误、错误中断、参数契约错误、外部服务故障和输出虚假完成。先定位 Trace，再决定修改提示词、确定性规范化、工具适配或状态转移；不能把每个失败都归结为“大模型不准”。

上线前还需扩展：真实多轮改写、跨任务信息污染、长上下文、真实 PostgreSQL/Redis/MySQL 断连恢复、长期压力与限流、首增量/最终完成时延、人工回复评分、真实复合事件拆分以及业务目标完成判断。
