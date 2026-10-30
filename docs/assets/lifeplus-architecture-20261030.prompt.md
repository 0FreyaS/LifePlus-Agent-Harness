# LifePlus 整体流程图生成说明

日期：2026-10-30。用途：GitHub README 前半部分，放置于全流程视频之后。

## 图的约定

- 以当前仓库的事件、全局规划、领域理解、Coordinator 和工具执行契约为依据。
- DeepSeek 负责文本事件、GoalPlan 及领域动作/工具/槽位；Jev 只承担普通单目标领域分类，异常或低分回退 DeepSeek。
- 三个独立分支示例为天气、路线与音乐；保存行程作为依赖路线的后继，前置成功后绑定真实回执。
- 结构化按钮不调用文本事件模型；恢复返回原任务节点，不重新规划。
- Tool Queue 是共享执行治理层；Checkpoint、记忆仓储及业务账本分别表示不同状态职责，不表示一个原子事务。
- 不画未部署的集群，不加入准确率、延迟、QPS 或未实现的训练模块。

## 生成方式

使用内置 image_gen 图像生成工具。该接口不提供可指定的模型参数；具体后端模型标识仅在实际结果可核验时记录，不把用户期望型号当作已验证型号。

## 最终交付与核对

- 仅交付 PNG，不生成 SVG、PDF 或 PPTX。
- 最终文件为 `lifeplus-architecture-20261030.png`，README 放置于全流程视频之后、各功能截图之前。
- 对原生成图做局部修订：保存任务的写入对象改为本地行程存储；移除易误读的跨卡片依赖箭头，改用“路线回执 → 保存行程 / depends_on + bind_slots”有向关系标注。
- 图为职责总览；不将框的相邻位置视作精确执行边，真实工具调用、状态字段及条件边以源码与架构执行指南为准。
- 已核对事件文本/按钮分流、Jev职责、全局不提参、本域understand、独立任务与后继保存、原节点恢复、队列/存储职责及不虚报导航播放能力。未加入测试指标。

## 完整提示词

```text
Use case: infographic-diagram
Asset type: GitHub README overall architecture image, Chinese with exact English code names.
Create one polished high-resolution LANDSCAPE architecture infographic for the actual software project "LifePlus Agent Harness". Prefer 3200 x 2100 pixels or a similarly sharp large landscape output. This is a README system overview, not a research figure or an app screenshot. Clean white background, crisp flat diagram, generous whitespace, dark charcoal readable typography, restrained emerald, blue and amber semantic accents. No gradient, 3D, robots, vehicle illustration, decorative circuitry, watermarks or fake metrics. Every label must be legible Chinese, code names spelled exactly. Small restrained line icons at most; prioritize diagram structure.

Visual contract: convey that language models propose bounded decisions, host code governs execution, independent tasks run concurrently, dependent tasks consume actual predecessor receipts. DeepSeek is the generative model (thinking disabled); Jev ONLY classifies an ordinary single-goal domain, with DeepSeek fallback. Do not depict Jev as the task planner or slot extractor. Use dark solid arrows for forward flow, blue dashed arrows for state/resume, amber arrows for actual dependency binding, with small legend.

Layout blueprint: a clear top-to-bottom reading path across three broad horizontal regions, with real module structure rather than six unrelated marketing cards.
Title upper-left: "LifePlus Agent Harness"
Subtitle: "多轮交互 · 并行任务 · 可恢复执行"

TOP REGION, heading "输入与决策":
Left: "用户 Query / 任务卡片" → "FastAPI" small subtitle "归属校验 · 请求去重".
After FastAPI two paths: "自由文本" → "DeepSeek 事件解释" → "宿主校验", and "结构化按钮" → directly same "宿主校验", bypassing event LLM.
Under event interpretation write compact exact labels "new / resume / cancel / clarify".
Host guard means task ownership, original text and candidate validity, NOT another language model.
Host new-demand output goes to "任务数量门控 · rules". From it two parallel decision paths:
"普通单目标" → "Jev 领域仲裁" small caption "低分 / 超时 → DeepSeek";
"疑似复合需求" → "DeepSeek · GoalPlan" small caption "目标 · 领域 · 依赖 · 结果绑定".
Both converge into the central scheduler below. Short label near multi-goal output: "不生成工具参数".
Old-task resume/confirm must BYPASS new planning, via a clearly routed blue dashed line to the waiting task in middle region. Cancellation/ambiguous cases can be a small terminal callout "取消 / 明确归属", not an executable business branch.

MIDDLE REGION dominant main feature, header "TaskCoordinator · DAG 调度" and a thin overarching frame "Agent Harness：预算 · 权限 · 状态 · 执行证据".
Show THREE equal-height side-by-side independent task lanes beginning at the scheduler with a shared bracket labeled "独立任务并行".
Three lanes:
1 "天气任务" / "独立 task_id / thread_id"
2 "路线任务" / "独立 task_id / thread_id"
3 "音乐任务" / "独立 task_id / thread_id"
Each lane contains a small domain execution step labeled "understand" with caption "本域动作 · 工具 · 槽位", then a step "领域工作流" with caption "准备 · 澄清 · 查询 · 选择 · 校验".
Within route lane show small amber pause marker "interrupt：等待选择". A blue dashed connection goes from pause to "用户补充 / 候选选择", and returns labeled "resume：原节点继续"; this is a side loop, not a restart at global planning.
Below route lane, draw a FOURTH dependent task labeled "保存行程" with small steps "understand → 参数确认 → 写入". Connect route receipt to this task using ONE clear amber arrow labeled "depends_on + bind_slots". The dependent task must not be shown as running alongside its unfinished route predecessor. Caption under this edge: "前置成功后，绑定真实路线回执". Its own understand happens after readiness; host merges bound_slots so a model cannot overwrite actual evidence. Use a small label "宿主合并 bound_slots" immediately inside this dependent task.
Small compact domain-graph label near lanes: "LangGraph：understand → prepare → clarify → execute → select → authorize → perform". This is a DOMAIN FLOW SUMMARY, do not claim all nodes call an LLM or that every task requires authorization. Read-only tasks pass authorization; writes need explicit confirmation.
Task lanes produce their results independently; no final barrier requiring all tools to finish before showing any progress.

BOTTOM REGION, heading "工具执行与结果交付":
One shared wide band "Tool Queue" caption "Schema / 权限校验 · 并发限制 · deadline · 有限重试 · 幂等 / Dead Letter".
Use clear vertical two-way connections between EACH lane's workflow and Tool Queue, including dependent save workflow. Tool Queue ⇄ "MCP Client / Server" ⇄ "高德地图 / 音乐检索 / 本地行程与提醒". Tools are invoked THROUGH queue, never direct LLM-to-MCP.
A right-side delivery band labeled "执行回执校验 → SSE" caption "进度 · 文字 · 路线卡片 · 执行 DAG", connecting back to user results; include a dashed progress connection from scheduler/workflows so SSE is not gated on all completion.
Separate compact storage boxes at base:
"Checkpoint" caption "图状态 · 中断 · 恢复"
"MemoryRepository" caption "短期上下文 · 长期偏好"
"SQLite 业务账本 / Trace" caption "任务 · job · 操作记录 · 事件"
Use dashed state links to scheduler and domain tasks. Do NOT imply all these stores are one transaction or that Postgres/Redis clusters are deployed.

Bottom small factual footer:
"7 个领域 · 11 个 MCP 工具"
"路线规划 ≠ 启动导航；音乐检索 ≠ 播放"
Legend: "实线：执行流  虚线：状态/恢复  橙线：数据依赖".
Keep diagram concise, readable, balanced. No accuracy percentages, latency or QPS figures, no BERT, no 400 tools, no training module, no distributed-cluster claims. No arbitrary extra agents or modules. Do not duplicate the top input block at the bottom. Strict spelling: LifePlus, LangGraph, DeepSeek, Jev, GoalPlan, TaskCoordinator, understand, bound_slots, depends_on, bind_slots, Tool Queue, MCP, SSE, Checkpoint, MemoryRepository, SQLite, Trace.
```
