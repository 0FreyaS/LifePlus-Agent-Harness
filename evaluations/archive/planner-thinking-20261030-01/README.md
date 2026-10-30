# 只为全局规划开启思考：关闭／low／high

真实DeepSeek Flash，30条原规划回归集＋3条同域目标合并诊断（后验案例，独立统计），每条各请求三种模式。单并发、打乱场景、六种调用次序轮换；同一原提示词、JSON契约、固定参考时间、校验及一次修复。只创建实验规划客户端，不修改.env、领域/回复模型、服务或Docker，不调用业务工具。

- 规划思考模式33条.jsonl与同名说明：原始输入、领域/依赖/绑定期望及scope。
- 逐条结果.jsonl：最终JSON、评分、异常、修复尝试、finish_reason和usage；不保存隐藏思考文本。
- 运行条件.json：实际Token上限、25秒请求超时、45秒累计规划预算、模型和源码hash。
- 实测汇总.json：30条主结果、3条诊断、配对修复与退化、推理及总Token；reasoning不重复相加。

本次是一轮回归，不是盲测或全链路任务完成率；缓存/供应商负载不受控，thinking模式下temperature不保证生效。参数以[DeepSeek官方文档](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode/)为依据。
