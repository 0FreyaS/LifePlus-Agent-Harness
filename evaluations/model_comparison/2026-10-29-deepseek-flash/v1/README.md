# Grok与DeepSeek Flash关闭思考模式：真实模型替换对比

日期：2026-10-29。A为原grok-4.3，B为deepseek-flash并显式关闭思考；planner/intent/response三角色均替换为相应组模型。本轮隔离试验当时未切换Docker及主.env；后续获批部署另记在docs/deepseek-rollout.md。

## 条件与边界

- DeepSeek独立API连通性探测200，返回模型deepseek-flash，显式thinking.type=disabled，探测无reasoning_content；探测用量9 Token单独存在，不混入配对任务消耗。
- 通过OpenAI兼容接口extra_body传入thinking；默认不设置时不改变原Grok请求。已用真实SDK的MockTransport验证该字段出现在HTTP请求体，不是仅提示模型不要思考。
- 两组均保留领域Jev，事件解释llm，单/多门控rules；相同Schema、提示词、上限、工具及存储实现。每场景每组独立进程与隔离数据库，串行交替AB/BA。
- 实际走HTTP处理器、Graph、MCP与外部天气/音乐；不改用户业务库。澄清及候选选择由固定测试策略回复，无真实用户思考等待，不包含浏览器/TCP展示。
- 天气与歌手介绍各重复3次，另有多任务及澄清各1例，共4类Query、8次场景运行、16条观察，不是8个独立真实用户。样本少，P95接近或等于最大值，不是生产SLA。
- 比较同时改变了模型、供应商与思考配置，不能把所有差异单独归因于关闭思考。Token采用各供应商分词和reported total，包含Jev及失败/重试；Token下降不直接等于货币成本下降。
- 完成判据为领域、关键参数、真实工具回执及澄清契约；天气不逐项核验气象事实，歌手介绍只检验领域与主题，不宣称已人工验证所有知识细节。

## 本轮配对结果

| 范围 | 完整通过/提交 Grok→DeepSeek | P50（秒） | P95（秒） | 总Token |
|---|---|---|---|---|
| 天气查询（同一句3轮） | 3/3 → 3/3 | 14.482 → 3.434 | 23.952 → 3.725 | 9643 → 5972 |
| 歌手介绍（同一句3轮） | 3/3 → 3/3 | 11.815 → 3.518 | 19.012 → 3.535 | ≥4945（1次未知） → 3382 |
| 天气＋音乐（1例） | 1/1 → 1/1 | 23.916 → 2.899 | 23.916 → 2.899 | 6122 → 3221 |
| 天气缺城市澄清（1例） | 1/1 → 1/1 | 26.460 → 3.853 | 26.460 → 3.853 | 3491 → 1985 |
| 合计8次场景运行 | 8/8 → 8/8 | 14.482 → 3.434 | 26.460 → 3.853 | ≥24201（1次未知） → 14560 |

完整8组P50降低76.29%，P95降低85.44%；分位数沿用项目nearest-rank口径。

用量完整的7组配对（包含两组Jev用量）：22650 → 13445 Token，减少40.64%。这只是已知用量子集；完整8组的Grok总量未知，不能给出精确全量降幅。

未知用量尝试（不剔除其任务时延或完成结果）：[{"id": "model-chat-r3", "arm": "baseline", "stage": "domain_route_precheck", "error_type": "TimeoutError", "elapsed_ms": 1502}]

其中Grok组歌手介绍第3轮的Jev超时后触发原LLM领域回退；其额外开销不是模型固有效果。天气3组均无此回退，仍有明显提速。

## 刚才历史单次结果（不是本轮A基线）

| 输入 | 历史Jev | 历史领域提参 | 历史回复 | 历史端到端 |
|---|---:|---:|---:|---:|
| 查询无锡今天天气 | 1.270秒 | 4.163秒 | 6.970秒 | 13.761秒 |
| 介绍歌手陶喆 | 0.769秒 | 无额外调用 | 6.639秒 | 7.594秒 |

历史数字来自本机data/jev-domain-rollout-smoke-20261029.json；用于对应之前讨论的现象，不与本轮多次重复混算。

## 本轮阶段耗时

| 单任务场景 | 模型阶段 | Grok P50 | DeepSeek P50 |
|---|---|---:|---:|
| 天气查询（同一句3轮） | domain_route_precheck | 1.017秒 | 0.734秒 |
| 天气查询（同一句3轮） | domain_understanding | 6.435秒 | 1.072秒 |
| 天气查询（同一句3轮） | response | 6.681秒 | 0.845秒 |
| 歌手介绍（同一句3轮） | domain_route | 6.992秒 | 未调用 |
| 歌手介绍（同一句3轮） | domain_route_precheck | 0.527秒 | 0.716秒 |
| 歌手介绍（同一句3轮） | response | 10.701秒 | 2.622秒 |

各阶段分位数不能直接相加成端到端分位数；多任务并行时尤其不能将所有模型耗时求和当关键路径。Jev未替换，两组Jev耗时差异是调用波动，不是DeepSeek优化了Jev。

## 文件与复跑

cases.jsonl是冻结输入与目标；results.jsonl保留逐条模型尝试、用量、完成判据失败；summary.json含各组P50/P95、分阶段与Token；manifest.json不含Key；details.csv便于筛选。私有完整Trace/SQLite及日志在evaluations/runs/deepseek-flash-ab-20261029-01。

私有.env.deepseek-ab已被Git忽略，600权限；不要上传或拷进镜像。本试验不自动部署；之后用户批准的DeepSeek切换与新的Jev基准对照属于独立后续操作。

```bash
.venv/bin/python -m evaluations.deepseek_model_ab --render
.venv/bin/python -m evaluations.deepseek_model_ab --run-live --public <新公开证据目录> --private <新私有日志目录>
```

思考模式参数依据：[DeepSeek官方说明](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)。
