# LifePlus 真实工具

`src/car_agent/tools/service.py` 提供参数模型、工具目录、上游适配与本地事务写入；`mcp_server.py` 仅负责将它们暴露为 MCP 工具。API、LangGraph 和测试使用相同实现。live 执行错误直接反馈，不补造成功结果。

## 工具目录

| 工具 | 数据来源/作用 | 主要参数 | 调用超时 |
|---|---|---|---|
| `maps_weather` | 高德真实城市预报 | `city`, `forecast_date` | 10秒 |
| `maps_search` | 高德地点检索 | `keywords`, `city` | 10秒 |
| `maps_route` | 高德驾车/步行/骑行路线 | `origin`, `destination`, `city`, `travel_mode` | 15秒 |
| `maps_local_search` | 周边饭店/KTV/电影院/咖啡店 | `category`, `anchor`, `city`, `radius_m`, `keyword`, `sort_by`, `min_rating`, `max_cost` | 15秒 |
| `maps_place_detail` | 实际 POI 对应商家详情 | `poi_id` | 10秒 |
| `search_music` | QQ音乐快速搜索 | `keyword`, `limit` | 10秒 |
| `save_itinerary` | 本地业务库真实写入 | `user_id`, `title`, `details`, `idempotency_key` | 5秒 |
| `create_reminder` | 本地业务库真实写入 | `user_id`, `title`, `remind_at`, `idempotency_key` | 5秒 |
| `list_itineraries` | 读取用户真实行程 | `user_id` | 5秒 |
| `list_reminders` | 读取用户真实提醒 | `user_id` | 5秒 |
| `cancel_reminder` | 取消待触发提醒 | `user_id`, `reminder_id` | 5秒 |

表中为队列单次调用预算，不是平均耗时。网络单请求超时为8秒；队列全局预算另计排队、尝试次数和退避时间。`GET /api/tools` 可查询目录和 JSON Schema。

## 参数和回执

```json
{"tool":"maps_search","arguments":{"keywords":"东南大学无锡校区","city":"无锡"}}
```

实际回执保留高德提供的 POI ID、名称、地址和坐标；不要根据名称自行生成坐标。地点搜索多个候选时，LangGraph 暂停并绑定候选快照。路线工具地址解析存在多结果时，返回 `AMBIGUOUS_LOCATION`、`field` 和 `requires_clarification`，编排节点再次暂停，补充明确地址后重新调用。

驾车与步行使用高德 v3 路线接口；骑行使用 v4 并解析 `errcode/data.paths`，不能用 v3 的 `status/route.paths` 去解析骑行。两端坐标已明确时跳过地理编码；返回距离、时间、分段指引和高德路线链接。路线规划成功不代表导航已在设备上启动。

起终点分别准备地址查询：清理起点“从……出发/由……启程”的口语包装；某端包含明确的“某市”且与共享 `city` 提示冲突时，不向该端施加错误的城市限制。不能把南京目的地的城市范围强加给无锡起点。没有明确城市冲突时保留原提示；地址多候选仍须澄清，不擅自取第一项。坐标统一检查范围，并规范到最多六位小数后再请求高德。

QQ音乐使用真实快速搜索接口，结果数量受平台候选限制，`limit` 是上限；返回歌曲 ID、歌手和官方详情链接，不补齐不存在的候选。检索与播放是不同能力，当前工具不直接控制播放器。

本地生活使用高德 `v3/place/around` 和 `v3/place/detail`。参考地点先解析为真实坐标，歧义需选取或补充地址。饭店使用中餐/外国餐/快餐/休闲餐饮类别，避免将奶茶与冷饮全部混入；KTV、影院、咖啡店使用各自官方分类。每次在按距离返回的首批最多20条候选内筛选排序，最多展示10条，不宣称全城评分最优。人均/评分未知时不满足对应显式约束；明确标注暂停营业的商家默认排除，不代表其他商家已核验实时营业。

商家 ID 从实际候选绑定，详情 ID 不一致直接拒绝。没有评分、人均、电话、照片和营业时间时保留为空；没有包厢/座位库存、电影排期、订票或预订能力。参考人均不是实时订单报价。照片使用高德返回的实际图片链接，不生成替代店面照片。

## 写入与提醒

保存行程和创建提醒由 LangGraph 确认节点控制。`user_id` 由服务端会话身份注入，不能由模型选择其他用户。参数校验后，在同一数据库事务中保存业务记录和工具回执。幂等范围为 `tool_name + user_id + idempotency_key`：同键同参返回原回执，同键异参返回 `IDEMPOTENCY_CONFLICT`，避免 `INSERT OR IGNORE` 掩盖参数冲突。

提醒要求未来的带时区 ISO-8601 时间，统一保存为 UTC，界面按本地时区显示。含糊、过去或不合法时间触发澄清，不会声称已创建。`ReminderScheduler` 每秒扫描到期提醒，在同一事务中写入唯一站内通知并将提醒标为 `delivered`；重启后补发到期提醒，重复扫描不重复创建通知。

站内通知保存在数据库，通过 `/api/notifications` 读取，在 `/life` 页面展示；支持已读状态。待触发提醒可以取消，已触发通知不能通过取消提醒撤回。服务停止期间不会实时发送通知，需要重启后补发。当前没有短信或第三方消息渠道。

## 错误与队列

- `INVALID_ARGUMENT`、`NOT_CONFIGURED`、`IDEMPOTENCY_CONFLICT` 等错误不重试。
- 网络超时、连接失败、部分服务限流可退避重试。
- `300xx` 保留实际失败阶段和上游错误信息，允许队列有限重试；错误参数须先纠正，不能依靠反复重试解决。回执包含不带 Key 的 `upstream_path`、`upstream_code`、`upstream_info`。
- 无结果与歧义不当作网络失败重试；歧义信息保留到图状态供澄清使用。
- 返回结构统一包含 `ok`、`elapsed_ms`，失败附 `error`、`error_code`、`retryable`。
- 每个调用有持久化工具任务与 Trace；参数与返回值遵守用户身份边界，不在错误文本中输出上游 Key 或带 Key 的 URL。

MCP stdio 生命周期由 owner task 管理。会话内独立工具调用并发派发，取消 Future 会取消对应调用，避免慢查询串行阻塞后续请求；创建和关闭 AnyIO 上下文仍在同一 owner task 中。

## 实际验证

运行 `python -m car_agent.tool_smoke` 可执行真实 MCP 集成验收，报告默认保存在 `data/tool-integration-latest.json`。包含工具发现、天气、POI、三种路线、音乐、并发读取、行程去重与冲突、用户隔离、提醒创建/取消/到期通知和非法时间拒绝。报告中的耗时是本轮样本，不用于声称线上 SLA。

完整工具验收现包含原 `30001` 跨城故障参数回归。`python -m car_agent.route_workflow_smoke --url http://127.0.0.1:8001` 另外验证真实模型驱动的起点澄清、校区选择和路线规划。复现过程见 [30001 修复记录](amap30001-fix.md)。

协议依据：[高德路线 API](https://developer.amap.com/api/webservice/guide/api/direction)、[QQMusicApi 搜索适配源码](https://github.com/L-1124/QQMusicApi/blob/main/qqmusic_api/modules/search.py)。
