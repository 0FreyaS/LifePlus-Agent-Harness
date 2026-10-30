# 纯回复过滤A/B：2026-10-29 v1

主测试文档：[持续维护记录](../../../docs/jev-front-ab-2026-10-29.md)。

- `datasets/events.jsonl`：36条输入、待回复任务、期望事件及期望二分类。
- `datasets/end_to_end.jsonl`：5个完整会话的目标及用户模拟器策略。
- `modules/events/`：模块cases/results JSONL、CSV、manifest、summary与变化量。
- `end_to_end/`：5组全局真实模型/MCP回执验证的可共享结果，移除归属与内部路径。
- `summary.json`：两组质量、P50/P95、全部Token、二分类覆盖、安全分母及失败归因。

报告复算：`python -m evaluations.resume_filter_report --local <原局部run目录> --global-run <原全局run目录>`。只处理现有证据，不调用模型；归档cases/results冻结，内容不一致拒绝覆盖。

A是自然文本直接LLM，不是当前规则优先基线。B只允许纯旧任务回复跳过LLM，不负责拆分复合事件。领域Jev和原数量规则固定一致。36个局部场景与5个全局场景分别报告，不能合并成41个独立线上会话或混算准确率。

所有失败、回退、未知usage保留；不包含原SQLite、认证或Key。原数据不修改，候选默认不启用。
