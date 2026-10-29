# Jev 三个前序模块：真实局部与全局 A/B

日期：2026-10-29。原模型 `grok-4.3`；决策模型 `jev-1.14.0`。本文是小样本试验记录，不是生产SLA或上线准确率。原系统默认配置未切换，运行中的Docker没有重启。

## 结论

1. **优先候选是单任务领域仲裁。** 它可以替代一次原LLM领域分类，不替代工具选择、槽位提取、澄清或结果校验。速度收益与Token收益必须分别看。
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

## 配对结果

耗时单位为秒，均计入失败。每行使用相同配对样本；Token包含Jev、原模型回退及格式修复，不是账单费用。

| 范围 | 完整通过/提交 | P50 | P95 | Token总计 |
|---|---|---|---|---|
| 领域仲裁21条 | 21/21 → 21/21 | 3.551 → 0.548 | 5.684 → 1.481 | 15161 → 16575 |
| 单/多目标判断30条 | 18/30 → 27/30 | 0.000 → 0.252 | 0.000 → 0.534 | 0 → 32046 |
| 事件开发：仅需模型的17条 | 16/17 → 16/17 | 5.038 → 6.344 | 23.980 → 16.666 | ≥23402（1次未知） → ≥28430（1次未知） |
| 事件扩展留出12条 | 12/12 → 10/12 | 8.164 → 11.152 | 18.722 → 17.340 | 16980 → 21561 |
| 全局完整任务12例 | 8/12 → 10/12 | 17.820 → 18.538 | 43.658 → 47.566 | ≥45674（3次未知） → ≥64892（1次未知） |
| 针对性链路验证4例（单独报告） | 3/4 → 4/4 | 17.905 → 15.823 | 24.164 → 21.964 | 15381 → 18503 |

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

- 优先扩大单目标领域路由的独立测试与线上影子流量，验证长尾、过期上下文、服务抖动和错误领域的拒判；目前不切换。
- 合并数量/领域判定值得继续测试；先完善不确定输入送生成式规划器的策略，再评估Token提示词裁剪。多目标生成式规划依旧是必要环节。
- 事件明确性审核的门槛与样本表现尚不支持启用；继续保持规则＋原LLM。含两处原文的新旧复合事件不能退化成只判单/多任务。
- 复合事件已经需要原LLM解释时，不在前面强加必然回退的Jev；全局计划已经分配领域时，不重复做单目标领域分类。下一轮可单独测试缩短分类提示词，但不能删掉跨域和否定约束来换Token节省。
- 规划超时、bind_slots非法等已有失败需单独修复和回归，不能把换分类模型当作解决这些问题。本轮为可归因对照不同时改它们。
- 当前默认仍为 `INPUT_EVENT_PROVIDER=llm`、`DOMAIN_ROUTE_PROVIDER=llm`、`PLANNING_GATE_PROVIDER=rules`；本地Key不进入报告、Git或Trace。

工程回归：700通过、1跳过（`data/jev-front-tests-20261029.xml`），覆盖可选前序的单次多问题契约、保留原默认、服务失败回退、完整原文方案及禁止授权绕过。受控测试不用于宣称真实模型准确率。

## 可复现命令

```bash
.venv/bin/python -m evaluations.jev_ab --run-live --split holdout --repeats 1 --output evaluations/runs/<new-domain-run>
.venv/bin/python -m evaluations.jev_gate_ab --output evaluations/runs/<new-cardinality-run>
.venv/bin/python -m evaluations.jev_event_ab --strategy jev_gate --output evaluations/runs/<new-event-dev-run>
.venv/bin/python -m evaluations.jev_event_ab --strategy jev_gate --front-holdout --output evaluations/runs/<new-event-holdout-run>
.venv/bin/python -m evaluations.jev_global_ab --candidate front --output evaluations/runs/<new-global-run>
.venv/bin/python -m evaluations.jev_global_ab --candidate front --dataset evaluations/data/jev-front-boundaries-v1.jsonl --output evaluations/runs/<new-boundaries-run>
.venv/bin/python -m evaluations.jev_front_report
```

以上真实试验会消耗供应商额度，目录必须使用新名称。运行本报告生成器不产生模型费用。原始SQLite、日志和结果在evaluations/runs对应目录；共享脱敏汇总位于[JSON证据](assets/jev-front-ab-20261029.json)。

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
