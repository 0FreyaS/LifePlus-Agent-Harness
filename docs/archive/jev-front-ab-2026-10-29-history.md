<!-- jev-ab:generated:start -->
# Jev 优化测试记录（持续维护）

测试基线日期：2026-10-29。原模型 `grok-4.3`；决策模型 `jev-1.14.0`。本文集中记录模块与全链路的正确率、耗时、Token、失败及边界；小样本结果不代表生产SLA。

**当前启用范围：** 经用户确认，仅启用`DOMAIN_ROUTE_PROVIDER=jev_cascade`；事件解释仍为`llm`、单/多任务门控仍为`rules`。[启用与本机验收](jev-domain-rollout.md)。本次维护不重新调用模型；以下仍是当时冻结的实测数据。

## 单任务领域仲裁（已启用）

范围：相同21条冻结领域样本；A为原`grok-4.3`领域分类，B为`jev-1.14.0`高置信度接受、其余回退同一原模型。下面统计B的全部尝试，不只统计Jev接受的快样本。

| 指标 | 原方案A | Jev级联B | 变化 |
|---|---:|---:|---|
| 完整通过/提交 | 21/21 | 21/21 | 正确数不变 |
| 领域准确率 | 100.00% | 100.00% | +0.00个百分点，持平 |
| P50耗时 | 3.551秒 | 0.548秒 | 减少3.003秒，下降84.57% |
| P95耗时 | 5.684秒 | 1.481秒 | 减少4.203秒，下降73.94% |
| 总Token | 15,161 | 16,575 | 增加1,414，上升9.33% |
| 平均每提交样本Token | 721.95 | 789.29 | 增加67.33 |

准确率 = 正确领域样本数 / 全部提交样本数；异常和超时计失败，回退后的正确结果计入级联系统准确率。P50/P95按nearest-rank计算，均包含失败与回退耗时。耗时下降率 = (A−B)/A；Token变化率 = (B−A)/A。
**结论：领域分类更快、准确率持平，但总Token增加，并未节省Token，也没有证明账单费用降低。** 不能把领域阶段84.57%的P50下降当作整条业务请求提速；21/21是此人工样本集结果，不是生产100%准确率。

B共有22次模型尝试：Jev分类21次、原领域模型回退1次。总Token包含这些回退。Jev单独接受样本的准确率与本表的最终级联准确率不是同一个分母。

[领域输入与期望](../evaluations/jev/2026-10-29/modules/domain/cases.jsonl) · [逐条CSV](../evaluations/jev/2026-10-29/modules/domain/details.csv) · [逐条JSONL](../evaluations/jev/2026-10-29/modules/domain/results.jsonl) · [汇总](../evaluations/jev/2026-10-29/modules/domain/summary.json)

## 统一测试数据目录

所有对应输入、期望、逐条结果、CSV、汇总与变化量整理在[统一归档](../evaluations/jev/2026-10-29/README.md)。原始运行目录、SQLite与完整日志继续保留，不移动、不删除。

| 数据范围 | 文件夹 | 场景数 / A+B观察数 |
|---|---|---:|
| 单任务领域仲裁 | [modules/domain](../evaluations/jev/2026-10-29/modules/domain) | 21 / 42 |
| 单/多目标判断 | [modules/cardinality](../evaluations/jev/2026-10-29/modules/cardinality) | 30 / 60 |
| 事件审核开发集 | [modules/events/development](../evaluations/jev/2026-10-29/modules/events/development) | 30 / 60 |
| 事件审核扩展留出集 | [modules/events/holdout](../evaluations/jev/2026-10-29/modules/events/holdout) | 12 / 24 |
| 前序联合方案全链路 | [end_to_end/front_combined](../evaluations/jev/2026-10-29/end_to_end/front_combined) | 12 / 24 |
| 联合方案针对性链路 | [end_to_end/front_boundaries](../evaluations/jev/2026-10-29/end_to_end/front_boundaries) | 4 / 8 |

新增的[纯回复过滤A/B数据](../evaluations/jev/2026-10-29-resume-filter/README.md)独立保存36条事件与5组全链路，结果追加在本文末尾独立实验区，不与上表旧版事件审核合并统计。

**整体测试范围必须区分：** `front_combined`的12例同时启用事件审核与合并前序判断，`front_boundaries`为另选的4例机制验证；都不是当前仅启用单目标领域Jev版本的全局A/B。该版本目前有2例Docker真实冒烟验收，但没有单独的全局配对统计，不能冒用联合方案数字。

**维护约定：** 每次记录完整通过/提交、准确率或完成率及其分母、P50/P95、全部Token与未知尝试、配置和代码/数据版本。正向与负向结果均保留，不把多个重叠集混算为整体。自动区由脚本复算，人工备注放文末保留区。

## 结论

1. **单任务领域仲裁已经获批启用。** 它可以替代一次原LLM领域分类，不替代工具选择、槽位提取、澄清或结果校验。速度收益与Token收益必须分别看。
2. **单/多任务判断适合继续验证。** 原实现是宽松正则粗筛，不是每次都用大模型分类；Jev增加一次低延迟模型调用，但可减少不必要的规划生成，也能识别规则漏掉的复合请求。
3. **事件明确性审核暂不建议启用。** 判为明确不等于拿到了任务ID、候选和新需求片段。此次使用宿主构造完整方案、Jev审核、不通过则原LLM解释的保守路径；回退过多，不能稳定减负。
4. **单次Jev可同时询问任务数量和单任务领域。** 不串行调用三个分类器；多任务仍必须由原规划器生成目标和依赖。减少原LLM调用不保证总Token下降。

领域局部P50下降84.6%，Token增加9.3%；事件扩展留出Token增加27.0%。合并前序的普通全局12例没有提速，9个用量完整配对Token增加16.7%；针对性4例P50下降11.6%，但Token增加20.3%。这些比例仅对应各自样本，不能跨样本替代或推广到生产。

## 实际测试方式

- 高确定性事件规则依然优先，零模型调用的路径不应为使用Jev而增加网络请求。候选无法唯一构造、歧义、超时及低置信度回退原解释器。授权不由Jev授予。
- 新开关 `INPUT_EVENT_PROVIDER=jev_gate` 用二选一审核唯一完整事件方案，与先前从多份候选选择的 `jev_cascade` 区分。
- `PLANNING_GATE_PROVIDER=jev_cardinality` 只判断目标数量；`jev_front` 在同一请求增加领域问题。单目标且领域通过门槛时，将原Query与领域传递下去，不生成参数。多目标调用原GoalPlan规划器。
- 固定1.5秒超时、confidence≥0.8及选中项概率≥0.9。沿用前轮门槛，没有根据本轮测试结果降阈值或修改业务提示词。概率不是未经验证即可宣称的真实准确率。
- 逐例交替AB/BA，真实请求模型；全局还走HTTP处理器、LangGraph、MCP及隔离SQLite。每例每组独立进程；自动回复模拟澄清/确认，不包含真实用户思考时间，不包含浏览器/TCP渲染。
- 此轮局部与全局runner有时间重叠，共享同一供应商配额，并非无干扰性能压测。保留429、超时和修复调用；小样本单次重复容易受网络与模型采样影响，不据此宣布显著提升。
- 领域21条为已有人工留出样本的复测；事件30条开发与12条扩展留出包含既有6条和本次新写6条；目标数量30条为人工编写的边界压力样本，不代表线上类别分布。没有训练或调参。

## 各模块与整体配对结果

耗时单位为秒，均计入失败。每行使用相同配对样本；Token包含Jev、原模型回退及格式修复，不是账单费用。

质量列对模块表示领域/数量分类正确率或完整事件契约通过率，对全局表示整场景全部目标完成率，不能混称为Jev独立准确率。

| 范围 | 完整通过/提交 | 准确率/完成率 | P50（秒） | P95（秒） | Token总计 |
|---|---|---|---|---|---|
| 领域仲裁21条 | 21/21 → 21/21 | 100.00% → 100.00% | 3.551 → 0.548 | 5.684 → 1.481 | 15161 → 16575 |
| 单/多目标判断30条 | 18/30 → 27/30 | 60.00% → 90.00% | 0.000 → 0.252 | 0.000 → 0.534 | 0 → 32046 |
| 事件开发：仅需模型的17条 | 16/17 → 16/17 | 94.12% → 94.12% | 5.038 → 6.344 | 23.980 → 16.666 | ≥23402（1次未知） → ≥28430（1次未知） |
| 事件扩展留出12条 | 12/12 → 10/12 | 100.00% → 83.33% | 8.164 → 11.152 | 18.722 → 17.340 | 16980 → 21561 |
| 全局完整任务12例 | 8/12 → 10/12 | 66.67% → 83.33% | 17.820 → 18.538 | 43.658 → 47.566 | ≥45674（3次未知） → ≥64892（1次未知） |
| 针对性链路验证4例（单独报告） | 3/4 → 4/4 | 75.00% → 100.00% | 17.905 → 15.823 | 24.164 → 21.964 | 15381 → 18503 |

**注意：** 原单/多目标判断本身只有正则，毫秒取整后为0，Token为0；Jev在这个局部是增加耗时和Token，不是提速。它的潜在收益来自后续少一次不必要的规划/路由，不可把60%粗筛命中率说成原系统端到端完成率。

事件开发集另有13条规则命中且不调用模型，两组全部通过。混合这13条零耗时会造成总体中位数对一条快样本极敏感，因此主表只列需要模型的17条，不使用混合总体P50宣传收益。

## 逐模块解释

### 事件：明确性不是二元标签就足够

例如‘第二个，帮我介绍南京天气’，正确事件必须包含：旧任务ID、第二个对应的快照、原文选择片段、完整的新需求片段。若音乐和地点都在等待且无焦点，不能猜一个。
宿主先按原文边界枚举并校验方案，合并语义等价的序数/候选名称；只有唯一方案才交Jev审核。Jev返回clear也必须重新通过任务归属、候选有效期、原文覆盖及权限校验。无法构造方案则直接调用原LLM，避免一次必定无收益的Jev。

- events_dev：1/30条被Jev直接接管；实际Jev调用9次。规则命中与无法构造方案不能算作Jev正确分类。
- events_holdout：1/12条被Jev直接接管；实际Jev调用6次。规则命中与无法构造方案不能算作Jev正确分类。

具体失败归因见文末：若没有Jev调用或Jev已回退，后续LLM随机格式失败不能归因于Jev做错事件标签；但仍计入候选系统完整失败率。此次不以降低阈值来掩盖回退成本。

### 单/多目标：减少误触发规划，不是取代规划

规则把逗号、‘和’等当疑似多任务。例如‘查无锡天气，谢谢’、坐标逗号、歌曲名《你和我》、‘和平路’、单目标的先检索再导航，都容易误触发。反过来，‘我要无锡明日天气外加周杰伦的歌’可能漏召回。
30条压力样本中粗筛18/30，Jev级联27/30，提升30个百分点。26条高置信度接管均正确；4条回退后有3条沿用规则误判。不是Jev高置信度把这3条判错。原规划器还可能纠正规则误触发，因此不是从60%提升到了90%的任务完成率。
低置信度回退样本包括‘提醒我明早八点带雨伞和钥匙’、‘查一查明天下午无锡下不下雨以及气温’和‘无锡天气周杰伦歌曲都要’。可考虑把不确定输入送生成式规划器，而非再次依赖粗筛；这会增加模型调用，需另做预先冻结数据的A/B，当前未启用。

### 单目标领域：最直接的一次模型替代

单领域分类只在有限的七领域与defer中选择，宿主固定Schema。最终工具与参数继续由领域understand生成和校验；多目标计划已经分派领域的子任务不重复分类。此次21条领域样本两组全部正确，是持平，不是准确率提高。

### 全局：不能按局部速度相加

候选组同时开启事件审核和合并前序分类，但不改原规划器、工具、参数、确认和回复。对每个任务都有真实planning.precheck事件，使用模型attempt验证调用路径生效。并发领域分支的耗时相互重叠，不能相加后当作端到端耗时。

12例实测虽然8/12→10/12完成，但P50反而17.820→18.538秒（增加4.0%），P95为43.658→47.566秒（增加9.0%）。不能宣布全局提速。两条完成数改善涉及原模型的事件超时与领域调用429，不等于Jev分类正确率提高16.7个百分点；需要更多隔离、重复试验。
为验证减少误触发规划，另外固定4个针对性案例：带‘谢谢’的天气请求、起点坐标逗号、歌曲《你和我》、没有明显连接词的天气＋音乐。它们来自机制定位，且在最初试验后选取，存在针对性选择偏差；与原12例分开报告，不将两者混合成更好看的总体。

| 全局阶段 | 原模型调用数 | 候选原模型调用数 | 候选Jev调用数 |
|---|---:|---:|---:|
| domain_route | 8 | 1 | 0 |
| domain_understanding | 11 | 13 | 0 |
| input_events | 2 | 2 | 0 |
| input_events_precheck | 0 | 0 | 2 |
| plan | 7 | 5 | 0 |
| planning_gate_precheck | 0 | 0 | 15 |
| response | 11 | 14 | 0 |

## Token与失败口径

优先读取供应商total_tokens，若只有input/output则相加；不重复加reasoning。任何尝试缺少usage，组总量记未知，只给已知下界，不把失败视为免费。

全局共同用量完整的配对有9组；仅这组固定交集可精确对比：38145 → 44498 Token。这不是全部12例的Token节省结论，未知尝试、被排除样本及其失败均保留在全体主表中。

以下失败全部保留在主结果中，归因只用于定位，不删除难例：

- `events_dev/llm14/candidate`：`{"id": "llm14", "arm": "candidate", "errors": [], "error_type": "HTTPException", "source": null, "attempts": [{"stage": "input_events_precheck", "provider": "typesafe", "elapsed_ms": 451, "error_type": null}, {"stage": "input_events", "provider": null, "elapsed_ms": 319, "error_type": "OpenAIRateLimitError"}]}`。
- `events_dev/llm14/baseline`：`{"id": "llm14", "arm": "baseline", "errors": [], "error_type": "HTTPException", "source": null, "attempts": [{"stage": "input_events", "provider": null, "elapsed_ms": 323, "error_type": "OpenAIRateLimitError"}]}`。
- `events_holdout/front06/candidate`：`{"id": "front06", "arm": "candidate", "errors": [], "error_type": "HTTPException", "source": null, "attempts": [{"stage": "input_events", "provider": null, "elapsed_ms": 6527, "error_type": "OutputParserException"}, {"stage": "input_events", "provider": null, "elapsed_ms": 5623, "error_type": "OutputParserException"}]}`。
- `events_holdout/holdout03/candidate`：`{"id": "holdout03", "arm": "candidate", "errors": ["route", "new_task_presence"], "error_type": null, "source": "llm", "attempts": [{"stage": "input_events", "provider": null, "elapsed_ms": 9767, "error_type": null}]}`。
- `global/multi_completion-04/baseline`：`{"id": "multi_completion-04", "arm": "baseline", "errors": ["goal1:wrong_domain", "goal1:leaf_not_completed", "goal1:missing_successful_tool_receipt:maps_weather", "goal2:missing_task", "unexpected_task_count"], "error_type": null, "source": null, "attempts": [{"stage": "plan", "provider": null, "elapsed_ms": 25540, "error_type": "OpenAITimeoutError"}]}`。
- `global/multi_completion-04/candidate`：`{"id": "multi_completion-04", "arm": "candidate", "errors": ["goal1:wrong_domain", "goal1:leaf_not_completed", "goal1:missing_successful_tool_receipt:maps_weather", "goal2:missing_task", "unexpected_task_count"], "error_type": null, "source": null, "attempts": [{"stage": "planning_gate_precheck", "provider": "typesafe", "elapsed_ms": 581, "error_type": null}, {"stage": "plan", "provider": null, "elapsed_ms": 26128, "error_type": "OpenAITimeoutError"}]}`。
- `global/jev-four-goals/candidate`：`{"id": "jev-four-goals", "arm": "candidate", "errors": ["goal1:wrong_domain", "goal1:leaf_not_completed", "goal1:missing_successful_tool_receipt:maps_weather", "goal2:missing_task", "goal3:missing_task", "goal4:missing_task", "unexpected_task_count"], "error_type": null, "source": null, "attempts": [{"stage": "planning_gate_precheck", "provider": "typesafe", "elapsed_ms": 953, "error_type": null}, {"stage": "plan", "provider": null, "elapsed_ms": 8033, "error_type": "OutputParserException"}, {"stage": "plan", "provider": null, "elapsed_ms": 9462, "error_type": "OutputParserException"}]}`。
- `global/jev-four-goals/baseline`：`{"id": "jev-four-goals", "arm": "baseline", "errors": ["goal1:wrong_domain", "goal1:leaf_not_completed", "goal1:missing_successful_tool_receipt:maps_weather", "goal2:missing_task", "goal3:missing_task", "goal4:missing_task", "unexpected_task_count"], "error_type": null, "source": null, "attempts": [{"stage": "plan", "provider": null, "elapsed_ms": 7285, "error_type": "OutputParserException"}, {"stage": "plan", "provider": null, "elapsed_ms": 8195, "error_type": "OutputParserException"}]}`。
- `global/event_completion-01/baseline`：`{"id": "event_completion-01", "arm": "baseline", "errors": ["HTTPStatusError:Server error '503 Service Unavailable' for url 'http://completion-eval/api/tasks/90b23463-3c66-4d5e-a024-404f6736804f/resume'\nFor more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/503"], "error_type": "HTTPStatusError", "source": null, "attempts": [{"stage": "domain_route", "provider": null, "elapsed_ms": 3983, "error_type": null}, {"stage": "domain_understanding", "provider": null, "elapsed_ms": 6616, "error_type": null}, {"stage": "input_events", "provider": null, "elapsed_ms": 25001, "error_type": "CancelledError"}]}`。
- `global/clarified_completion-29/baseline`：`{"id": "clarified_completion-29", "arm": "baseline", "errors": ["goal2:wrong_domain", "goal2:leaf_not_completed", "goal2:missing_successful_tool_receipt:search_music"], "error_type": null, "source": null, "attempts": [{"stage": "domain_route", "provider": null, "elapsed_ms": 10259, "error_type": null}, {"stage": "domain_understanding", "provider": null, "elapsed_ms": 10205, "error_type": null}, {"stage": "domain_route", "provider": null, "elapsed_ms": 402, "error_type": "OpenAIRateLimitError"}, {"stage": "response", "provider": null, "elapsed_ms": 4545, "error_type": null}]}`。
- `boundaries/front-implicit-multi/baseline`：`{"id": "front-implicit-multi", "arm": "baseline", "errors": ["goal2:missing_task", "unexpected_task_count"], "error_type": null, "source": null, "attempts": [{"stage": "domain_route", "provider": null, "elapsed_ms": 8078, "error_type": null}, {"stage": "domain_understanding", "provider": null, "elapsed_ms": 4747, "error_type": null}, {"stage": "response", "provider": null, "elapsed_ms": 3539, "error_type": null}]}`。

## 下一步与配置边界

- 单目标领域路由已启用，继续扩大独立测试，验证长尾、过期上下文、服务抖动和错误领域的拒判；其他Jev试验不启用。
- 合并数量/领域判定值得继续测试；先完善不确定输入送生成式规划器的策略，再评估Token提示词裁剪。多目标生成式规划依旧是必要环节。
- 事件明确性审核的门槛与样本表现尚不支持启用；继续保持规则＋原LLM。含两处原文的新旧复合事件不能退化成只判单/多任务。
- 复合事件已经需要原LLM解释时，不在前面强加必然回退的Jev；全局计划已经分配领域时，不重复做单目标领域分类。下一轮可单独测试缩短分类提示词，但不能删掉跨域和否定约束来换Token节省。
- 规划超时、bind_slots非法等已有失败需单独修复和回归，不能把换分类模型当作解决这些问题。本轮为可归因对照不同时改它们。
- 当前本机与配置模板为 `INPUT_EVENT_PROVIDER=llm`、`DOMAIN_ROUTE_PROVIDER=jev_cascade`、`PLANNING_GATE_PROVIDER=rules`；本地Key不进入报告、Git或Trace。

工程回归：700通过、1跳过（`data/jev-front-tests-20261029.xml`），覆盖可选前序的单次多问题契约、保留原默认、服务失败回退、完整原文方案及禁止授权绕过。受控测试不用于宣称真实模型准确率。

## 可复现命令

```bash
.venv/bin/python -m evaluations.jev_ab --run-live --split holdout --repeats 1 --output evaluations/runs/<new-domain-run>
.venv/bin/python -m evaluations.jev_gate_ab --output evaluations/runs/<new-cardinality-run>
.venv/bin/python -m evaluations.jev_event_ab --strategy jev_gate --output evaluations/runs/<new-event-dev-run>
.venv/bin/python -m evaluations.jev_event_ab --strategy jev_gate --front-holdout --output evaluations/runs/<new-event-holdout-run>
.venv/bin/python -m evaluations.jev_global_ab --candidate front --output evaluations/runs/<new-global-run>
.venv/bin/python -m evaluations.jev_global_ab --candidate front --dataset evaluations/data/jev-front-boundaries-v1.jsonl --output evaluations/runs/<new-boundaries-run>
```

仅归档与重建报告，无付费API调用：

```bash
.venv/bin/python -m evaluations.jev_archive
.venv/bin/python -m evaluations.jev_front_report
```

上面真实试验命令会消耗供应商额度，目录必须使用新名称。归档和报告生成器只读既有证据，不产生模型费用；没有私有原run目录时可使用统一归档复算。原始SQLite、日志和结果在evaluations/runs对应目录；共享脱敏汇总位于[JSON证据](assets/jev-front-ab-20261029.json)。
<!-- jev-ab:generated:end -->

## 后续测试记录（人工维护）

此区保留人工备注；报告重建不会覆盖。新增试验记录日期、范围、数据版本、结论与证据目录，不改写历史实测值。

### 2026-10-29：建立统一维护与数据归档

- 将21条领域仲裁结果置顶，补充准确率100.00%→100.00%（0个百分点）、P50下降84.57%、P95下降73.94%、总Token增加1,414（9.33%）。
- 六组数据导出至`evaluations/jev/2026-10-29/`，包括109个组内场景、218条A/B观察；不混算为109个独立用户会话。
- 保留旧原始数据库、日志和Trace；未重新调用模型，未修改本机已启用的单任务Jev路由。
- 增加本地复算、未知Token、冻结数据拒绝覆盖及人工备注保留的回归检查。未来新实验追加新版本，而不是改写本轮结果。
- 本次维护后工程回归706通过、1跳过，记录在`data/jev-archive-tests-20261029.xml`；该数量不是模型准确率。

<!-- jev-planner-fallback:v1:start -->

### 2026-10-29：Jev低分回退LLM规划器（中文标记）

本轮实现：Jev高置信度保持原分流；低分、defer和服务异常直接调用原LLM规划器，不再退回正则。系统无法预知Jev是否正确，因此不按事后标签决定回退。当前单任务领域Jev保持原样；未自动开启单/多门控或重启Docker。

| 范围 | 完整通过/提交 A→B | P50（秒） | P95（秒） | 总Token |
|---|---|---|---|---|
| 原30条回归 | 27/30 → 30/30 | 0.293 → 0.335 | 1.404 → 7.088 | 32046 → 37177 |
| 新增12条留出 | 10/12 → 12/12 | 0.248 → 0.254 | 0.549 → 10.636 | 12825 → 16657 |

原30条回归Token增加5,131（16.01%）。
新增12条留出Token增加3,832（29.88%）。


[本轮数据、逐条结果与复算说明](../evaluations/jev/2026-10-29/modules/cardinality_llm_fallback/v1/README.md)。原始用例、模型输出、CSV、统计分别保存在该目录；旧版数据未覆盖。

本轮42条每条共享一次真实Jev调用，B在7条不确定输入上真实调用原LLM规划器。这是模块策略对比，不是完整工具链路测试；正确率改善但P95与Token增加，不能宣称提速。工程回归717通过、1跳过（本机data/jev-planner-fallback-tests-20261029.xml）；独立待发布快照709通过、2跳过。受控回归不等于模型质量。

<!-- jev-planner-fallback:v1:end -->


<!-- resume-filter-v1:start -->
## 纯旧任务回复过滤实验：直接LLM vs Jev过滤＋LLM

日期：2026-10-29。A=`INPUT_EVENT_PROVIDER=llm_direct`，主输入框的live自由文本不走原事件快规则，直接由原LLM输出InputDecision；B=`jev_resume_filter`，只对纯旧任务回复尝试跳过该LLM。卡片索引、确认、拒绝和取消仍走原宿主协议。卡片中手动输入的文本仍是文本，不是结构化按钮。

两组都保留原文覆盖、状态/候选有效期、任务归属和权限校验。B先判断resume_only/needs_llm：只有高分resume_only且宿主能够唯一绑定整句话时，才恢复旧任务。携带任何新需求、否定、授权、取消或不确定含义，一律仍需原LLM；Jev不生成新任务片段。
例如‘无锡校区吧’可只恢复旧任务；‘无锡校区吧，顺便查下明天无锡天气’必须由LLM拆成恢复＋新需求。新需求由现有API创建独立业务任务，若本身多目标再由Coordinator拆子任务，不虚称为直接往旧图中插入一个节点。

### 冻结数据与测试条件

- 新建36条：纯回复12、复合回复12、边界12；边界含过期、多列表歧义、否定、引用、取消、非法序号及待审批。标注在真实调用前冻结，未根据结果改提示词、门槛或标签。
- 新建5个真实全链路场景：歌曲纯选择、天气补城市、两种恢复＋新任务、一个结构化卡片对照。文本回复从主输入框提交，不借用卡片focus作弊。
- 原模型grok-4.3、Jev固定jev-1.14.0；门槛confidence≥0.8、概率≥0.9、Jev超时1.5秒、事件总预算25秒，回退/修复调用都计入。
- 两组全局均固定领域jev_cascade、数量门控rules，仅改变事件策略。当前本机数量门控本来就是rules，本轮未启用或测试新的数量策略。
- 随机场景顺序，交替AB/BA，每例一次；先运行局部、结束后才运行全局，避免两类runner争抢供应商额度。手工构造小样本，有模板复用，不代表线上SLA。
- 局部只测试真实模型与事件契约，不调用业务工具；全局使用真实模型、Graph、MCP、隔离存储与自动用户回复，不包含人工思考/浏览器渲染。确认类数据的clarify是当前保守策略，不是唯一可行产品策略。

- 局部‘南京校区/无锡校区’是人工候选快照，不是本轮高德实时回执；5例真实链路覆盖天气和音乐，不宣称已经覆盖高校实际POI简称消歧。宿主只接受完整匹配的候选/序号和有限城市回复，无法安全绑定的简称、自由起点等仍回退LLM。

### 局部完整事件结果

| 范围 | 完整通过/提交 | 准确率/完成率 | P50（秒） | P95（秒） | Token总计 |
|---|---|---|---|---|---|
| 全部事件36条 | 34/36 → 36/36 | 94.44% → 100.00% | 7.468 → 7.966 | 16.125 → 14.696 | ≥54008（1次未知） → ≥71052（3次未知） |
| 纯旧任务回复12条 | 12/12 → 12/12 | 100.00% → 100.00% | 5.821 → 6.354 | 9.495 → 11.818 | 15640 → ≥21245（1次未知） |
| 恢复＋新需求12条 | 12/12 → 12/12 | 100.00% → 100.00% | 7.274 → 8.684 | 11.246 → 12.208 | 17156 → ≥25872（1次未知） |
| 边界12条 | 10/12 → 12/12 | 83.33% → 100.00% | 8.933 → 7.866 | 25.023 → 20.147 | ≥21212（1次未知） → ≥23935（1次未知） |

完整正确要求route、任务ID、候选索引、旧回复与全部新需求片段正确；不是仅看Jev二分类标签。失败和超时不剔除。A原本规则能处理的纯回复也调用LLM，因此不能把这张表的收益说成相对当前规则优先系统的收益。

纯回复组A总Token为15640，B已知下界21245、另有1次未知usage，因此至少增加35.84%；这是消耗增加的下界，不是精确总量。复合组A总Token为17156，B已知下界25872、另有1次未知usage，因此至少增加50.80%；这是消耗增加的下界，不是精确总量。未知usage不得当作0。

补充零模型规则对照：现有规则在同一12条纯回复中直接处理10条，其中正确10条。未命中的部分没有在这个对照中调用LLM，因此不提供伪造的规则系统整体时延或准确率。这个对照只说明直接LLM不是当前服务基线，不是额外一轮付费A/B。

### Jev覆盖与安全边界

- 纯回复接管：2/12；其余回退原LLM。
- 实际返回二分类的请求：31；其中标签匹配期望29，分母不包含没调用或服务失败的请求。这个标签正确率不替代全系统通过率。
- 复合/边界误走纯恢复捷径：0/24。这是本批受测次数，不保证未来永久为0。
- 事件原LLM尝试次数：40 → 34；候选还调用Jev 34次。减少原LLM不等于所有模型调用数或Token都减少。

这次局部只有2条被Jev接管明确省去事件LLM；原LLM尝试次数其余差异还包括A组格式修复次数，不能全部当成Jev带来的节省。

局部例子‘无锡校区吧’：5.752→0.462秒，候选实际来源为jev。单例收益不能代表整个纯回复组，未过既定门槛仍回退；测试期间没有为了让结果更好而降低阈值。

### 真实全链路结果

| 范围 | 完整通过/提交 | 准确率/完成率 | P50（秒） | P95（秒） | Token总计 |
|---|---|---|---|---|---|
| 5个完整会话 | 5/5 → 5/5 | 100.00% → 100.00% | 31.142 → 28.012 | 58.551 → 49.250 | ≥34802（2次未知） → ≥36565（1次未知） |

时延包含初始创建任务、真实工具、文本事件解释、恢复及所有新任务完成，不只是回复过滤耗时；两组初始新请求都需要原事件LLM。结构化卡片对照仍包含初始模型耗时，不把整场会话误称为零模型。

**关键归因：** 5组全局的事件LLM调用次数为9→9，B额外调用事件Jev 4次。原领域LLM回退次数为2→0；回复模型耗时也有波动。因此不能仅凭全局P50变化宣称过滤器节省了事件LLM，需结合下面的阶段记录。

| 阶段 | A调用数 / 尝试时长合计 | B调用数 / 尝试时长合计 |
|---|---:|---:|
| input_events | 9 / 67.459秒 | 9 / 66.381秒 |
| input_events_precheck | 0 / 0.000秒 | 4 / 3.427秒 |
| domain_route | 2 / 12.330秒 | 0 / 0.000秒 |
| response | 7 / 51.319秒 | 7 / 46.889秒 |

时长合计跨多个会话及模型尝试，仅用于定位工作量，不是单请求P50/P95，也不将并行分支相加当作端到端时延。

### 失败证据与解释

- `boundary04/baseline`：`{"id": "boundary04", "arm": "baseline", "source": null, "group": "boundary", "errors": [], "error_type": "HTTPException", "decision": null, "prechecks": [], "attempts": [{"stage": "input_events", "provider": null, "elapsed_ms": 4784, "error_type": "OutputParserException"}, {"stage": "input_events", "provider": null, "elapsed_ms": 5595, "error_type": "OutputParserException"}]}`。
- `boundary05/baseline`：`{"id": "boundary05", "arm": "baseline", "source": null, "group": "boundary", "errors": [], "error_type": "HTTPException", "decision": null, "prechecks": [], "attempts": [{"stage": "input_events", "provider": null, "elapsed_ms": 17170, "error_type": "OutputParserException"}, {"stage": "input_events", "provider": null, "elapsed_ms": 7826, "error_type": "CancelledError"}]}`。

本轮边界请求的通过数差异如发生在原LLM解析、超时或连接阶段，不应解读为Jev二分类修好了这些问题；两组都使用同一个原LLM，单次采样存在波动。

### 维护与复跑

本轮只产出证据，不自动改主输入框策略或启用过滤器。检查分组耗时、全部Token、接管率及原规则覆盖后再做启用决策；整体偶然更快不能替代对事件阶段的归因。后续可独立比较‘规则已能完整绑定就直接恢复、真正需要语义判断时再用Jev’，但不把未经测试的改动写成已生效。

本实验不切换本机/容器默认，不自动更新GitHub。已启用的单领域Jev和其他服务配置保持不变。低分回退、服务异常和原LLM错误分别归因，不把回退错误说成Jev二分类错误。未知usage记null/下界，不能据此推断费用节省。

[本实验数据与逐条证据](../evaluations/jev/2026-10-29-resume-filter/README.md)。原始audit SQLite和全局Trace保留在对应evaluations/runs。

```bash
.venv/bin/python -m evaluations.jev_event_ab --strategy jev_resume_filter --baseline-policy llm_direct --dataset evaluations/jev/2026-10-29-resume-filter/datasets/events.jsonl --output evaluations/runs/<new-local-run>
.venv/bin/python -m evaluations.jev_global_ab --candidate resume_filter --dataset evaluations/jev/2026-10-29-resume-filter/datasets/end_to_end.jsonl --output evaluations/runs/<new-global-run>
```

上述命令调用真实付费API，使用新目录，不覆盖旧记录。报告导出不发起模型调用。
<!-- resume-filter-v1:end -->

<!-- model-comparison:deepseek-flash:start -->

### 2026-10-29：deepseek-flash关闭思考，对比原grok-4.3

领域Jev与业务流程保持不变，只替换原LLM三个角色；这轮隔离测试当时不自动切换服务，之后获批切换另记于deepseek-rollout.md。真实模型＋Graph＋MCP，共8组配对。

| 范围 | 完整通过/提交 Grok→DeepSeek | P50（秒） | P95（秒） | 总Token |
|---|---|---|---|---|
| 天气查询（同一句3轮） | 3/3 → 3/3 | 14.482 → 3.434 | 23.952 → 3.725 | 9643 → 5972 |
| 歌手介绍（同一句3轮） | 3/3 → 3/3 | 11.815 → 3.518 | 19.012 → 3.535 | ≥4945（1次未知） → 3382 |
| 天气＋音乐（1例） | 1/1 → 1/1 | 23.916 → 2.899 | 23.916 → 2.899 | 6122 → 3221 |
| 天气缺城市澄清（1例） | 1/1 → 1/1 | 26.460 → 3.853 | 26.460 → 3.853 | 3491 → 1985 |
| 合计8次场景运行 | 8/8 → 8/8 | 14.482 → 3.434 | 26.460 → 3.853 | ≥24201（1次未知） → 14560 |

本组不是此前Jev候选策略的A/B；天气/聊天重复各3次，另有多任务与澄清各1例。关闭思考通过HTTP请求字段实现，不是提示词。结果不等于已证明知识回答100%准确或生产SLA。

用量完整的7组配对（包含两组Jev用量）：22650 → 13445 Token，减少40.64%。这只是已知用量子集；完整8组的Grok总量未知，不能给出精确全量降幅。

Grok组歌手介绍第3轮Jev超时并回退LLM，已保留该次完整耗时；不能把这部分差异全归因于DeepSeek。

[完整阶段分解、历史单次对照、数据与限制](../evaluations/model_comparison/2026-10-29-deepseek-flash/v1/README.md)。

<!-- model-comparison:deepseek-flash:end -->

<!-- deepseek-jev-v1:start -->

## DeepSeek基准下的Jev实测（2026-10-29）

LLM统一deepseek-flash关闭思考，固定模型/提示词/阈值；领域与事件分别配对，不把Grok历史收益当成DeepSeek收益。

| 范围 | 完整通过/提交 A→B | P50秒 | P95秒 | 总Token |
|---|---|---|---|---|
| 领域21条 | 21/21 → 21/21 | 0.748 → 0.302 | 1.316 → 1.232 | 4494 → ≥15428（1次未知） |
| 领域全局12例 | 10/12 → 10/12 | 3.731 → 3.265 | 7.057 → 6.699 | 29965 → ≥34293（1次未知） |
| 自由文本事件36条 | 34/36 → 34/36 | 0.852 → 1.216 | 1.655 → 2.004 | 24659 → ≥46850（1次未知） |
| 其中纯回复12条 | 12/12 → 12/12 | 0.777 → 1.068 | 1.655 → 1.816 | 8115 → 14342 |
| 其中恢复＋新任务12条 | 12/12 → 12/12 | 0.814 → 1.280 | 1.050 → 2.199 | 8220 → ≥16513（1次未知） |
| 文本事件全局5例 | 5/5 → 5/5 | 5.452 → 7.595 | 7.918 → 8.699 | 19873 → ≥22511（1次未知） |

纯回复Jev接管4/12；复合/边界误走捷径0/24。接管率与最终准确率分别统计。
具体例子：‘无锡校区吧’事件理解1.061→0.395秒、B来源jev；‘无锡校区吧，顺便查下明天无锡天气’0.807→0.932秒、两组均走LLM并完整保留新需求。上述为本批对应单例，不推广为总体收益。
失败归因：领域全局两组都在天气结果保存、四目标依赖规划两例发生结构化解析失败；事件两组都在取消和带引号词义提问上过度澄清。所有失败留在分母，不归因为Jev修复或退化。
事件A为直接DeepSeek，不是当前规则优先基线；实验事件策略不自动启用。小样本单次，失败不剔除、未知用量不当0，不承诺生产SLA。

[分组结果、具体两例、阶段耗时、全部数据与失败证据](../evaluations/jev/2026-10-29-deepseek/v1/README.md)。

<!-- deepseek-jev-v1:end -->

<!-- llm-direct-rollout:v1:start -->

## 后续批准：自由文本直接交给DeepSeek

2026-10-29，用户批准模板和本机采用`INPUT_EVENT_PROVIDER=llm_direct`。自由文本不先走事件规则或事件Jev，模型提案仍经过宿主校验；结构化选择、确认、拒绝、取消不走文本模型。领域Jev及数量门控rules保持不变。该选择依据直接LLM相对Jev过滤的结果，不宣称优于零模型规则路径。

切换后独立单臂回归5/5完整会话通过，9次事件DeepSeek、0次事件Jev；本机Docker真实认证HTTP/SSE天气和歌手介绍2/2完成。新验收不并入旧A/B指标。[新回归数据](../evaluations/jev/2026-10-29-deepseek/v1/rollout/llm-direct/summary.json)。

### 12例全局中失败的两例

- `multi_completion-04`：查询无锡天气后将完整回执保存到行程，原计划输出`bind_slots={"result":"t1.result"}`，错误地把前置结果名用作后继接收字段；合法接收字段是`details`。两组均在一次修复后仍未通过GoalPlan校验，尚未执行工具。
- `jev-four-goals`：天气、音乐、行程列表并行，保存仅依赖天气。原日志被300字符上限截断，只能确认两次规划解析失败；本次独立planner诊断完整复现相同的非法目标字段`result`。这不是原始响应的无损恢复，诊断不混入旧测试统计。

正确契约为`depends_on=["t1"]`、`bind_slots={"details":"t1.result"}`。JSON本身可解析，失败在业务语义校验；不是高德接口错误，也不是Jev领域分类错误。本次不修改绑定规划逻辑、不删除失败、不把10/12改写为12/12。[具体原文、证据边界及改进方向](llm-direct-rollout.md) · [独立诊断完整输出](../evaluations/jev/2026-10-29-deepseek/v1/diagnostics/planning-20261029.json)。

<!-- llm-direct-rollout:v1:end -->
