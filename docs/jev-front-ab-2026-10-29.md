<!-- lifeplus-test-report:redirect -->

# LifePlus测试记录入口

当前测试统一以DeepSeek Flash关闭思考为基准，领域、槽位、规划、澄清、文本事件、完整任务、工具与工程回归集中维护。

- [测试总文档](../测试回报/测试总文档.md)
- [测试数据集及逐文件说明](../测试回报/测试数据集/README.md)

实测、历史模型无关证据、验收目标与待测项分开，不把旧模型效果迁移为当前指标。原完整记录已归档，原始数据不删除。

维护命令：`.venv/bin/python -m evaluations.test_report`（离线整理，不请求付费API）。
