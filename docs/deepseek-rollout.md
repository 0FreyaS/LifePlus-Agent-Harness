# DeepSeek Flash模型切换与Jev对比

2026-10-29，用户批准将基础模型从`grok-4.3`切换为`deepseek-flash`，关闭思考模式，并将代码、配置模板和脱敏实测结果推送GitHub。真实Key仅存在本地私有环境文件中。

## 生效配置

```dotenv
MODEL_BASE_URL=https://api.deepseek.com/v1
MODEL_NAME=deepseek-flash
MODEL_API_KEY=<仅在私有环境中填写>
MODEL_THINKING=disabled
PLANNER_MODEL_NAME=
INTENT_MODEL_NAME=
RESPONSE_MODEL_NAME=
DOMAIN_ROUTE_PROVIDER=jev_cascade
INPUT_EVENT_PROVIDER=llm_direct
PLANNING_GATE_PROVIDER=rules
```

三个角色不再保留旧模型覆盖值，统一继承`MODEL_NAME`；单任务领域仲裁仍由此前已获批的Jev级联负责，低分或失败回退DeepSeek。领域工具选择、参数提取、规划和回复仍由各自LLM角色处理，不把Jev描述为可执行工具的生成式Agent。

`build_models`在HTTP请求中传入`extra_body={"thinking":{"type":"disabled"}}`，不是仅用提示词要求“不思考”。默认Settings不设置该字段，兼容其他供应商；模板选择DeepSeek后才显式关闭。切回不支持该参数的供应商时，应删除`MODEL_THINKING`。参数依据：[DeepSeek思考模式文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)。

## 三类结果分别看

1. [Grok与DeepSeek模型替换配对](../evaluations/model_comparison/2026-10-29-deepseek-flash/v1/README.md)：8组，两边保留领域Jev；用于判断底层模型切换的整体差异，不能说明Jev相对DeepSeek的贡献。
2. [DeepSeek基准下Jev领域与事件对比](../evaluations/jev/2026-10-29-deepseek/v1/README.md)：21条局部领域、12个领域全局场景、36条文本事件与5个事件全局场景；分别配对，避免混淆改动。
3. [持续维护测试记录](jev-front-ab-2026-10-29.md)：保留旧Grok基准、模型替换及新DeepSeek基准，不修改历史数字。

领域实验A为DeepSeek分类，B为Jev分类＋DeepSeek回退。事件实验A为自由文本直接DeepSeek，B为Jev纯回复过滤＋DeepSeek；两组事件实验均保留领域Jev。实验当时服务为规则优先；用户随后批准采用A（`llm_direct`），B仍不启用。[最新启用与失败分析](llm-direct-rollout.md)。

## 部署与验证

私有`.env`由Compose运行时注入，镜像及Git中不保存Key。修改环境变量需要重建容器，单纯`docker compose restart`不会重新读取`env_file`：

```bash
docker compose up -d --build --force-recreate app
docker compose ps
```

通过受保护的`/api/health`核对`model_name=deepseek-flash`，再用真实对话的`model.attempt.*`记录确认实际模型名与工具回执。健康检查只证明进程与配置，不等于供应商可用或任务完成；闭环验收还需真实SSE请求。

本机旧模型配置备份在未跟踪的`data/model-before-deepseek-20261029.json`，权限600；旧镜像标签为`lifeplus-agent:before-deepseek-20261029`。这些是本机回滚材料，不上传GitHub。回滚需恢复相应私有配置和镜像后重建容器，不删除数据卷；不能仅恢复模型名却留下DeepSeek URL/Key/思考参数。

### 本机切换验收

本机Compose服务`http://127.0.0.1:8088`已重建并为healthy。容器内客户端核对`model_name=deepseek-flash`、`thinking=disabled`及实际`extra_body`；真实认证HTTP＋SSE的天气、歌手介绍两例均completed，模型Trace确认DeepSeek执行、领域Jev保留，天气有真实工具回执。耗时分别4.020秒、3.189秒，仅为两例部署冒烟，不加入配对P50/P95。

初版冒烟脚本直接读取completed事件的model_name，因该字段可能仅存在started事件而断言报错；已按span_id合并started/completed后复验通过。该次是验收采集错误，不是业务任务执行失败；不把修正采集后的重复请求混进A/B。

本次全新Dockerfile构建实际失败于PyPI下载`pybase64`三次重试后超时。当前部署复用本机旧镜像中已可用的依赖与前端，叠加待提交源码快照；不宣称已证明空白服务器全新安装成功。后续服务器部署仍需重新验证依赖下载、完整镜像构建与入口导入。旧镜像及持久卷未删除。

部署验收的脱敏摘要见[rollout-acceptance.json](../evaluations/model_comparison/2026-10-29-deepseek-flash/v1/rollout-acceptance.json)；原始本机记录为未跟踪的`data/deepseek-rollout-smoke-20261029.json`。

待发布隔离快照完整工程回归为**785通过、2跳过**，本机JUnit为`data/deepseek-final-snapshot-tests-20261029.xml`；思考开关、HTTP真实序列化、完整原文绑定、复合回复不走捷径及结构化卡片绕过文本解释均有受控测试。跳过项不当作通过，工程回归不代表模型准确率；源码之外的后续报告补充未改变运行逻辑。

## 实测边界

样本均为人工编写的回归/机制测试，不代表真实流量分布。全局走真实模型、Graph、MCP与隔离存储，排除浏览器展示和用户思考时间；通过判据核对领域、参数和回执，不宣称完整人工知识质量审查。按供应商reported total统计所有调用与回退，未知usage保留为未知，Token不是账单费用。

两个模型在既有复杂依赖样本中都有规划Schema解析失败的历史记录；本轮不在A/B进行中修改提示词、绑定契约或测试标签来抬高完成率。失败样本继续保留，不以简单天气/闲聊成功替代复杂依赖验收。
