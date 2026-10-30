# Jev 测试数据与证据归档：2026-10-29

统一测试记录：[jev-front-ab-2026-10-29.md](../../../docs/jev-front-ab-2026-10-29.md)。本目录汇集该文档六组试验的冻结Query、期望、逐条结果和汇总；不运行模型，不改变当前服务。

| 范围 | 数据目录 | 场景数 | A/B观察数 | 质量口径 |
|---|---|---:|---:|---|
| 单任务领域仲裁 | [modules/domain](modules/domain) | 21 | 42 | 领域准确率 |
| 单/多目标判断 | [modules/cardinality](modules/cardinality) | 30 | 60 | 数量分类正确率 |
| 事件审核开发集 | [modules/events/development](modules/events/development) | 30 | 60 | 完整事件契约通过率 |
| 事件审核扩展留出集 | [modules/events/holdout](modules/events/holdout) | 12 | 24 | 完整事件契约通过率 |
| 前序联合方案全链路 | [end_to_end/front_combined](end_to_end/front_combined) | 12 | 24 | 会话全部目标完成率 |
| 联合方案针对性链路 | [end_to_end/front_boundaries](end_to_end/front_boundaries) | 4 | 8 | 会话全部目标完成率 |

## 每组文件

- `cases.jsonl`：当时冻结的输入、上下文或等待任务、期望与业务目标，不从当前代码猜测旧标签。
- `results.jsonl`：baseline/candidate逐条结果，保留失败、超时、回退、模型attempt、供应商usage；移除归属与运行路径。
- `details.csv`：便于筛选的逐条对照表，包含原Query、期望、实际、通过、耗时、Token与错误。
- `summary.json`：分子分母、正确率/完成率、nearest-rank P50/P95、分阶段用量与混淆矩阵。
- `comparison.json`：准确率变化（百分点）、耗时及Token绝对/相对变化。耗时/Token正数代表增加，不自动称为收益。
- `manifest.json`：原试验范围、日期、模型/门槛及来源；不含私有Key、数据库或HTTP认证。

## 数据边界

这六组共109个组内场景、218条A/B观察，存在场景/模板复用，不能称为109个独立用户或混算总体正确率。事件开发的17条需模型子集从30条原集派生，不重复存储或计数。
领域21条的最终准确率包含Jev不接受时的LLM回退，不等同于Jev独立分类准确率。总体试验是前序联合方案，不是当前单独启用领域Jev版本的整体配对证据；针对性4例与普通12例分开。
未知usage保留null，CSV记unknown，已知消耗仅为下界。原SQLite、完整Trace和日志仍保留在被Git忽略的evaluations/runs；本目录是可共享、可复算的导出，不移动或删除原始记录。

## 维护方式

```bash
.venv/bin/python -m evaluations.jev_archive
.venv/bin/python -m evaluations.jev_front_report
```

上述命令只处理现有文件，不产生模型费用。Query和逐条结果冻结，内容变化时拒绝覆盖，须建立新的日期/版本目录；可修复汇总算法后重建派生文件。原run目录不存在时可直接使用本归档复算。
以后新实验先冻结数据和对照范围，运行后追加新版本，记录模型、配置、代码版本、统计单位、失败与未知用量，不改旧标签迎合模型输出。其他历史试验仍以旧报告及原run目录为准，不静默合并进本轮。
