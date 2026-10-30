# LifePlus 分层架构图备选版

用途：按用户提供的分层流程图风格生成第二张 PNG，供选择；不替换当前 README 图片。

参考来源：本轮用户主动上传的架构图，仅参考横向分层、彩色标题带、细线图标及右侧观测栏的视觉语言。旧图中的 Noim/BERT/XAI/车控等内容不作为当前事实。

工具：内置 image_gen；不提供可指定或核验的具体模型参数。仅交付 PNG，不生成可编辑格式。

输出：`lifeplus-architecture-layered-v2.png`。已检查分层顺序、文本/按钮分流、Jev与DeepSeek职责、Coordinator与领域子图边界、暂停恢复、依赖保存、工具队列和右侧观测闭环。它是分层职责示意，不替代源码的逐节点执行图；不含测试数字。README当前图片保持不变，待用户选择。

## 完整提示词

```text
Generate a NEW high-resolution landscape Chinese architecture infographic for "LifePlus Agent Harness". The attached image is a USER-PROVIDED STYLE REFERENCE ONLY, not a target to preserve and not a factual specification. Borrow its broad readable design language: stacked horizontal architecture layers, compact line icons, crisp thin colored outlines, colored header tabs and a right-side observability column, white background, dark navy headings, green execution arrows and orange human-interaction accents. Produce an original composition adapted to LifePlus. Replace ALL legacy Noim/car content with the verified structure below. Do not copy the reference's exact boxes, text, topology, BERT, car control, XAI, Query rewrite, circuit breaker or autonomous release claims. This is a README visual, not an app screenshot. No photographs, gradients, 3D, watermark or fake metrics. Aim at a sharp wide image around 3200×1800; readable Chinese labels, no microtext.

TITLE centered: "LifePlus Agent Harness 整体流程架构"
SUBTITLE: "LangGraph × DeepSeek × Jev × MCP × Memory × Human-in-the-loop × Trace"
Use exact spelling and canonical case. Do not invent acronym expansions.
Layout: four wide horizontal regions on the left about 84% width, one aligned narrow vertical region on the right about 16%. Main reading direction left-to-right in each region, top-to-bottom across regions. Labels short, visual hierarchy clear, no overlapping arrows.

REGION 1, emerald header "1. 用户请求与事件入口":
A clear left-to-right chain:
"用户 Query / 任务卡片" (文本输入 / 结构化按钮)
→ "FastAPI" (用户与会话归属)
→ "request_id 去重" (重复提交 / 冲突检查)
→ "事件解释" (自由文本：DeepSeek / 按钮：宿主协议)
→ "宿主校验" (原文片段 / task_id / 候选有效期)
→ a compact four-way event outcome, exact short labels "new / resume / cancel / clarify".
Mark: new goes to new-goal planning in region2; resume bypasses new planning and continues existing task node; cancel stops the specified task; clarify asks which task/action when ambiguous. Model suggestions never equal permission. Use an orange dashed HUMAN loop, not a new global planning loop.

REGION 2, blue header "2. 任务编排与领域执行":
This is the largest region, containing TaskCoordinator OUTSIDE per-task LangGraph, never label all planning as a LangGraph node.
First small module "上下文准备" (近期历史 / 有效引用 / 用户偏好).
Then branching module "新目标判定" with two visibly separate alternatives:
"普通单目标：Jev 领域仲裁" (低分或异常 → DeepSeek)
"疑似复合：DeepSeek GoalPlan" (目标 / 领域 / 依赖 / bind_slots; 不生成工具参数)
Both feed "TaskCoordinator" (校验 DAG / 调度就绪任务 / 真实回执绑定).
Then one clearly bounded "LangGraph 领域子图：独立 task_id / thread_id" area with three visually parallel mini-lanes:
"天气任务", "路线任务", "音乐任务".
Each has its own short "understand" marker meaning 本域动作 / 工具 / 槽位. Under lanes, a compact shared text "prepare → clarify → execute → select → authorize → perform".
Within route lane add "interrupt：候选选择" and a small external human box "用户补充 / 选择" linked by dashed orange outbound and return arrows labeled "resume：原节点继续". Do not route resume back through Planner.
Inside this domain area, add a fourth dependent mini-task "保存行程（依赖路线）". It starts ONLY after route completion, consumes real route receipt via host-owned bound_slots and requires write confirmation. Communicate dependency with a compact textual statement, exactly "路线回执 → 保存行程" and smaller "depends_on + bind_slots"; DO NOT draw a misleading arrow from music to save or a two-way dependency.
At the far right of the region, two small output modules "执行回执校验" → "回复 / 记忆更新". The result side explicitly permits each independent task to finish separately. SSE can push progress before all tasks finish.

REGION 3, restrained violet header "3. Agent Harness 运行与治理":
One horizontal strip of nine compact, aligned modules; these are cross-cutting guards, not a sequential tool loop:
"工具白名单" (领域候选范围)
"Schema 校验" (字段 / 业务约束)
"权限与确认" (写操作绑定参数)
"Tool Queue" (持久作业 / 并发)
"Deadline / Retry" (超时预算 / 有限重试)
"累计预算" (模型 / 工具 / 恢复次数)
"幂等与账本" (去重 / 未知结果核查)
"Checkpoint" (图状态 / 中断)
"Interrupt / Resume" (补充 / 选择 / 确认)
Thin upward connectors indicate governance applies to region2. Do not invent Hooks, circuit breaker, sandbox execution, distributed workers or permission guarantees.

REGION 4, green header "4. 模型、记忆与真实业务能力":
Keep model/context resources separate from the tool protocol. Left-side resource modules with no false serial arrows between unrelated stores:
"DeepSeek Flash" (关闭思考 / 事件·规划·提参·回复)
"Jev" (单目标领域分类 / DeepSeek 回退)
"MemoryRepository" (短期上下文 / 长期偏好)
"RAG" (本地健康知识)
Right-side tool area:
"Tool Queue" → "MCP Client / Server" branching to:
green outlined group "只读查询" with "高德地点 / 路线 / 天气 / 商家" and "音乐检索";
orange outlined group "需确认的写操作" with a shield-like user confirmation icon and "保存行程 / 创建提醒 / 取消提醒".
The MCP protocol does not itself grant permissions. All actual business tools go through queue and host checks. Do not show models directly invoking arbitrary tools.
A small separate base storage caption: "默认 SQLite；PostgreSQL Checkpointer、Redis/MySQL 记忆可选". No cloud cluster drawings.
No car navigation launch, vehicle actuators, booking, media playback or medical diagnosis capabilities.

RIGHT REGION 5, blue header "5. 可观测与迭代":
A vertical evidence workflow with short connectors:
"Trace 事件流" (模型 attempt / 工具 job / 节点迁移)
→ "执行数据与 DAG" (并行 / 依赖 / 等待)
→ "Bad Case 归因" (事件 / 规划 / 参数 / 工具 / 回复)
→ "冻结回归集" (失败回流 / 版本隔离)
→ "人工复核与发布决策" (不自动启用优化).
A small dashed return edge to Harness/planning labeled "人工确认后迭代". Do not show implemented LLM-as-Judge scoring, automatic self-training, auto-release or any metrics.

LEGEND at bottom:
"实线：执行 / 数据流   橙色虚线：人工补充与恢复   蓝色虚线：状态 / 事件"
Small capability footer: "7 个领域 · 11 个 MCP 工具 | 路线规划 ≠ 启动导航；音乐检索 ≠ 播放"
Keep every module grounded in this specification. Use compact precise Chinese, clean line icons only, generous margins, no cropped content. It must look like an intentional system blueprint, not a simple Mermaid export and not a clone of the reference.
```
