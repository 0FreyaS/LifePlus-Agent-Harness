# LifePlus 架构图片生成说明

- 日期：2026-10-26。
- 用途：技术学习文档的三层编排架构概览，不是运行 Trace 或测试报告。
- 生成方式：内置 image_gen 工具；接口未返回可核实的模型版本，不能确认标注为 GPT Image 2。
- 交付格式：PNG；根据用户选择，保留 Markdown 内 Mermaid，不额外重建 SVG/PPTX。
- 结构依据：TaskCoordinator、build_graph、build_domain_graph、ToolQueue 等当前仓库源码。
- QA：检查任务级并发、B→D 的实际结果依赖、领域子图顺序及 interrupt/resume；图片简化了失败分支和基础设施支撑连线，精确控制条件以正文 Mermaid 和源码为准。
- 权限/Schema 在提交队列前校验，MCP 服务端再校验；图片底层治理带不表示这些检查只发生在出队后。

## 首次生成完整提示

```text
Create a detailed technical architecture diagram for a Chinese software engineering learning document, not a marketing poster. Landscape 3:2 high resolution. Title: “LifePlus · 三层任务编排与执行架构”. White background, readable dark Chinese text, precise connectors, flat engineering style, restrained blue/teal/amber semantic accents, no decorative illustrations, no gradients, no invented metrics. Give adequate whitespace and legible large labels.

The core fact: asynchronous TaskCoordinator outside LangGraph schedules independent tasks; each child task invokes its own stateful LangGraph workflow with independent thread_id; domain subgraphs handle clarification/selection/approval through interrupt/resume. Do NOT draw a single parent LangGraph fan-out as if that were actual implementation.

Layout:
Top full-width input band: 用户 Query / UI 操作 -> FastAPI 接入 -> 请求去重 · 归属校验. A separate short “补充 / 选择 / 确认” path leads to “恢复指定 task_id + interrupt_id”, not to creating all tasks again.

Middle left large panel named “第一层 · TaskCoordinator 任务级编排”.
Chain: “多任务检测” -> “LLM 结构化计划” -> “Schema / DAG 校验” -> “就绪任务调度”.
From scheduler fork to independent blue task A “查天气”, independent teal task B “地点检索 → 用户选择 → 路线规划”, independent amber task C “音乐检索”.
Place A/B/C on parallel lanes. Task D “保存行程 · 写入确认” depends ONLY on completed B; draw explicit B -> D arrow labeled “实际路线回执”. A and C have no edges to D. Lanes merge at “父任务聚合：完成 / 部分完成 / 等待 / 失败”. Clear annotation “独立任务并行；依赖满足才启动”.

Middle right panel named “第二层 · 每个任务独立运行 LangGraph”.
Main graph exactly “memory → route → 领域子图 → respond”.
Add label “独立 thread_id / Checkpoint”.
Inside domain subgraph a clear mini-flow, exactly:
“prepare” -> “clarify” -> “execute” -> “select” -> “authorize” -> “perform”.
Explain below with short aligned Chinese labels: 规则与参数 / 缺参澄清 / 查询或准备 / 候选选择 / 写入确认 / 执行写入.
Clarify, select and authorize have dashed amber connectors to “interrupt → 等待用户 → resume”, returning to relevant paused node. Do not route all six steps through LLM. Annotate “只读任务跳过实际写入；失败提前结束”. Main respond after domain graph: “回执校验 → 回复”.

Bottom full-width panel named “第三层 · Harness 工具执行与基础设施”.
Flow: “Tool Queue” -> “权限 + 参数校验” -> “本地 stdio MCP” -> “高德 / 音乐 / 行程与提醒”.
Tool Queue small sublabel “并发限制 · 超时预算 · 有限重试 · Dead Letter”.
Three support groups below:
1 “上下文与记忆：Redis 可选 / MySQL 可选 / SQLite 回退”
2 “状态持久化：Checkpoint：PostgreSQL 可选 / SQLite 回退；业务与队列：SQLite”
3 “可观测：事件 Trace → SSE → 对话页 / 执行 DAG”
Dashed support lines connect checkpoint to per-task graph; traces receive task and tool events. Solid arrows mean execution/data dependencies; dashed mean persistence/events/user pause, provide small legend.
Small footer “路线规划 ≠ 导航启动；Checkpoint ≠ 外部操作回滚”.
Keep module names exactly; no autonomous multi-agent claims; no Skill count, no 400 tools, no production throughput claims.
```

## 连线校正提示

```text
Edit this technical architecture figure, preserving its overall three-tier layout, typography, colors and clear domain-subgraph flow. Correct these important factual connector problems, without adding decoration:
1. In the top input band, remove the horizontal arrow labeled “新任务请求（创建并执行任务）” pointing from “请求去重 · 归属校验” into “恢复指定 task_id + interrupt_id”. New task requests must ONLY flow down into first-tier TaskCoordinator. The recovery box must have its own independent small input labeled “补充 / 选择 / 确认”, then a down arrow into the second-tier LangGraph. No edge between new-task admission and recovery.
2. Bottom infrastructure: remove the checkpoint dashed arrow currently pointing into “本地 stdio MCP”. Instead connect “状态持久化” directly upward to second-tier label “独立 thread_id / Checkpoint”; label this dashed connector “图状态读写”. Draw no checkpoint persistence edge into the tool/MCP chain.
3. Draw execution into Tool Queue from second-tier domain execute/perform boundary, not from TaskCoordinator scheduler as if it directly calls tools. Draw trace collection dashed lines into the bottom “可观测” group, NOT into the upstream “高德 / 音乐 / 行程与提醒” box.
4. Replace first input FastAPI sublabel “身份认证” with “本地身份 · 会话管理 · 请求解析” because production auth is NOT implemented. In parent task independent music lane replace “检索目的地相关音乐” with just “独立音乐查询”; in weather replace “查询目的地天气信息” with “独立天气查询”. No implied data dependency on place search.
5. In mini domain graph keep original exact edges prepare→clarify→execute→select→authorize→perform→respond, and independent optional interrupt/resume beside clarify/select/authorize. Keep “只读任务跳过实际写入；失败提前结束”.
6. Keep A/B/C parallel and ONLY B→D dependency from actual route receipt. Preserve all other supported labels.
Ensure connectors never overlap text; high resolution, flat white engineering diagram.
```

## 最终支撑连线简化提示

```text
Make a final minimal edit to this LifePlus engineering diagram. Keep every box and all text, the entire top area and main blue execution arrows.
Important: simplify bottom support wiring to remove misleading endpoints. Remove ALL dashed navy or orange connectors BETWEEN the second-tier area and the bottom infrastructure area. Specifically erase the “图状态读写” dashed connector and label, and erase the “任务与工具事件上报 (Trace Events)” orange dashed connector and its label. These currently point to the wrong boxes. Do not redraw any replacement cross-panel dashed connectors.
Keep the solid blue execution wire from domain graph to Tool Queue.
Keep orange dashed interrupt/resume arrows INSIDE the second-tier domain-subgraph panel.
The bottom three support boxes should be freestanding with no wires, and the state box should contain explicit short text “为每个任务读写 Checkpoint”. The observability box should contain “接收任务与工具事件”. These labels explain support semantics without ambiguous arrows.
In bottom legend change the dark dashed legend description to “支撑能力见底层说明” or remove that legend entry if no corresponding line remains. Preserve solid blue execution and orange user-interaction legend entries.
Everything else unchanged, Chinese exact, no new claims, high resolution.
```

