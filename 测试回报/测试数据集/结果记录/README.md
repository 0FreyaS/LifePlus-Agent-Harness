# 结果文件用途

- domain.jsonl：D01，DeepSeek领域分类与Jev级联配对。
- events.jsonl：D08，直接DeepSeek与Jev纯回复过滤配对。
- domain_e2e.jsonl：D09，领域开关真实全链路配对；事件为当时规则优先。
- events_e2e.jsonl：D10，文本解释策略全链路配对。
- current.jsonl：D10，当前llm_direct部署决定后的单臂复测，不是新A/B。
- basic.jsonl：D11，仅保留DeepSeek的8条观察，包含同Query重复。
- historical_components.jsonl：D12–D15的2026-10-28组件测试；不调用理解模型，版本旧于当前，不能替代当前模型效果。
- 汇总.json：由逐条结果复算的质量、时延、Token、混淆矩阵与工程回归索引。未知usage为null，已知下界另列。

配对结果与部署复测有样本复用，不相加宣传独立样本量；不包含SQLite、完整会话库、API Key或认证数据。
