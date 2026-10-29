# 项目架构记录

更新日期：2026-10-29。本文为仓库内持久记录，供后续开发和面试说明查阅；以当前源码和测试为依据。

## 当前启用决策

2026-10-29后续用户批准基础LLM切为`deepseek-flash`，`MODEL_THINKING=disabled`通过HTTP参数关闭思考；planner/intent/response继承同一模型，不留Grok角色覆盖。保留`DOMAIN_ROUTE_PROVIDER=jev_cascade`、`INPUT_EVENT_PROVIDER=llm`、`PLANNING_GATE_PROVIDER=rules`，事件纯回复过滤仍仅实验。此前Grok测试属于历史基准，不能外推为DeepSeek上的Jev收益。[切换、复现与回滚](deepseek-rollout.md)及[DeepSeek基准配对证据](../evaluations/jev/2026-10-29-deepseek/v1/README.md)分别记录。真实Key不进入Git。

2026-10-29用户明确批准先启用单任务领域仲裁并同步GitHub。本地`.env`和仓库`.env.example`为`DOMAIN_ROUTE_PROVIDER=jev_cascade`、`INPUT_EVENT_PROVIDER=llm`、`PLANNING_GATE_PROVIDER=rules`。需要独立JEV_API_KEY；低分、defer和异常回退原LLM。未显式配置provider时Settings类仍用llm兼容旧部署。仅改变领域分类，其他实验不启用；以下“默认不切换”是此前试验当时的状态，不能用来推翻本次批准。[启用说明](jev-domain-rollout.md)。

本机Docker服务8088已更新，真实天气/歌手介绍两例均completed，Trace确认Jev仲裁且无原domain_route调用；Jev耗时分别1.270/0.769秒，不能当端到端SLA。回归700通过、1跳过。本机基础镜像下载EOF，改用现有依赖加源码层运行，保留旧镜像回滚；未验证全新服务器镜像安装。Jev代码与脱敏实测报告已提交GitHub main，实际Key不提交。

## 当前事实

- 项目名称为 LifePlus，提供生活健康与出行服务；当前七领域、十一个 MCP 工具，不沿用座舱量产、十二领域、四百工具或日均十万请求的未验证规模。
- 自然语言入口先做事件理解：高确定性规则优先，复杂输入由 LLM 提出 `new/resume/cancel/clarify`，宿主核对任务归属、原文片段、候选、有效期及授权。事件分类先于业务规划，不负责自由生成工具参数。
- 普通单目标通过主图 `route` 做领域仲裁；复合请求由全局模型输出 `GoalPlan`。每项目标只有 `id/query/domain/depends_on/bind_slots`；全局不选工具、不抽取槽位。内部 `TaskPlan` 的旧字段是兼容结构。
- `TaskCoordinator` 校验 DAG、并发推进就绪任务，依赖任务等前置成功后读取真实 Checkpoint/回执。`depends_on` 约束何时运行，`bind_slots` 定义数据来源。
- Coordinator 将前置证据放入 `prepared_decision.bound_slots`。领域 `understand` 加载宿主允许的候选工具及 Schema，联合选择动作、工具并提取本领域槽位；随后宿主合并绑定字段，真实回执优先于模型生成值。
- `chat/wellbeing` 使用固定内部动作，避免额外提参模型调用；结构化表单、路线方式切换、已验证的上下文引用和中断恢复有各自的可信路径。
- 缺参、实体消歧、候选选择与写操作确认在领域流程中处理。Checkpointer 保存图状态，Tool Queue 和操作回执约束实际执行；Checkpoint 不自动撤销外部操作。
- 当前是带领域子图的有状态工作流与任务协调器；领域子图本身不等同于自治多 Agent。没有已接入 BERT Top-K 的证据，不描述为运行中的两级 BERT 路由。
- [分层实现说明](domain-execution-routing.md)中的真实模型测试受超时和 429 影响；没有证据证明新版准确率、P95 或费用已经改善。历史指标只适用于当时的实现和条件。

## Jev 接入状态

已按用户提供的 [TypeSafe 官方 Quickstart](https://docs.typesafe.ai/introduction/quickstart)核对。

- 独立 HTTP API：`POST https://api.typesafe.ai/v1/systemone`，不能直接作为聊天模型的 OpenAI-compatible URL 使用。
- 单目标 `domain_route` 在七领域与 `defer` 中选择。事件解释还可使用 Jev 从宿主构造的合法 `InputDecision` 候选中选择；候选覆盖不了完整含义时回退原模型，不要求 Jev 自由生成原文片段。
- 默认 `DOMAIN_ROUTE_PROVIDER=llm`、`INPUT_EVENT_PROVIDER=llm`。用户已于2026-10-29授权真实测试并提供 Key，Key 保存在未跟踪的本地 `.env`，官方真实接口已验证。默认服务与现有容器不自动切换。
- 现有全局规划、领域工具与槽位联合提取、用户授权和执行结果校验继续负责各自职责。
- 受控 HTTP 测试与真实 Jev 配对试验分别记录。领域开发集14组、留出集21组，以及事件开发集30组、留出集6组已经实际请求两类模型；全链路按12个独立场景交替AB/BA运行，结果见对应实测报告。
- 失败、超时、回退的调用与用量均纳入；缺失用量记未知。grok-4.3 的 total_tokens 可能包含单列的推理用量，汇总优先采用供应商 total_tokens，不能仅加输入和可见输出，也不能重复加 reasoning。
- 全局首轮01发现每任务RunContext遗漏domain_router，不计作双模块对比。已改为dataclasses.replace完整继承上下文，并增加实际Graph与Trace生效断言。对原模型和候选同时启用stream_usage，记录流式回复用量，正式重跑保存在02目录。首轮原始记录与exclusion说明保留。
- [真实局部与全局结果](jev-ab-2026-10-29.md)列明各组通过率、P50/P95、已知与未知用量及失败归因；不自动切换默认provider。
- 已完成02轮12组配对：两组均11/12完成，P50为22.270→16.953秒，P95为41.042→36.384秒；候选四次无usage，不能确认精确Token节省。两组四目标失败分别是原规划模型格式错误和连接错误，不归因到Jev。
- 局部领域留出集两组21/21，P50为4.889→0.582秒；事件开发集需模型子集P50为5.780→6.054秒，留出集6/6→5/6。因此领域判断值得继续验证，事件分支不建议直接切换，默认保持llm。全量回归684通过、1跳过；小规模实测不代表生产SLA。

## 前序三模块实验补充（2026-10-29）

- 新增可选`INPUT_EVENT_PROVIDER=jev_gate`：规则命中直接返回；未命中时宿主构造并去重完整事件方案，唯一时由Jev审核clear/unclear，不明确、无唯一方案或失败仍用原LLM。不能用“明确”标签代替任务归属、原文拆分或授权。
- 新增`PLANNING_GATE_PROVIDER=jev_cardinality/jev_front`；默认`rules`。`jev_front`一次HTTP询问目标数量和单目标领域，高分单目标绕过生成式全局规划并传递领域，多个目标仍生成GoalPlan；defer沿用原启发式路径。多目标拆分、绑定及领域槽位仍由原模型处理。
- 本轮重新真实测试：领域21条两组21/21，P50 3.551→0.548秒，Token15161→16575（增加9.3%）；单/多目标30条边界压力样本18/30→27/30，P50 0→0.252秒，Token0→32046。后者只评分类门控，不是系统完成率；26条高分正确，4条回退后仍有3条粗筛错分。
- 事件开发需模型17条16/17→16/17，P50 5.038→6.344秒；两边有429。扩展留出12条12/12→10/12，P50 8.164→11.152秒，Token16980→21561。Jev仅直接接管开发/留出各1条，其余保留规则或回退。两条留出失败均在原LLM回退中，仍计入候选系统失败。
- 合并前序＋事件审核全局12例8/12→10/12，但P50 17.820→18.538秒、P95 43.658→47.566秒，未证明提速。两例完成数改善涉及原事件超时与领域429，不能宣称模型准确率净提升。用量完整交集9例38145→44498 Token；全12例两组均有未知用量，不能声明精确总消耗。原规划超时与非法绑定两组仍失败。
- 追加4例针对性验证（与12例分开）：3/4→4/4，P50 17.905→15.823秒，Token15381→18503。无明显连接词的复合请求原系统遗漏音乐，新门控触发规划后完整完成。此组有事后针对性选择，不作为无偏总体质量/速度结论。
- 局部与12例runner曾重叠共享供应商配额，不是无干扰压测；保留失败、单次重复和小样本限制。详见[新报告](jev-front-ab-2026-10-29.md)、脱敏JSON、原始`evaluations/runs/jev-*20261029-01`。默认服务/容器未切换，优先候选仍是领域仲裁，不启用事件审核。
- 增量模块、传输契约、回退、默认值及评测口径全量回归700通过、1跳过，记录在`data/jev-front-tests-20261029.xml`；受控回归不是模型质量的证据。

## 定位入口

| 内容 | 依据 |
|---|---|
| 事件分类 | `src/car_agent/input_events.py` |
| 全局目标契约 | `src/car_agent/task_planning.py::GoalPlan` |
| 依赖证据绑定 | `src/car_agent/orchestration.py::TaskCoordinator._bind` |
| 领域 Schema 与工具候选 | `src/car_agent/domain_understanding.py` |
| 两层理解与图执行 | `src/car_agent/harness.py::resolve_intent / resolve_domain_intent` |
| Jev 传输与门控 | `src/car_agent/adapters/jev.py`、`src/car_agent/domain_routing.py`、`src/car_agent/event_routing.py` |
| 模型角色及配置 | `src/car_agent/model_roles.py`、`src/car_agent/config.py` |
| Jev 方案、评测口径与命令 | [Jev 接入与 A/B 方案](jev-integration-and-ab.md) |

后续架构变化需同步此记录、对应设计说明和相关测试。架构示意、验收目标、受控测试和真实服务实测分别表述。
