# 2026-10-26 发布准备验证

## 已完成

| 范围 | 结果 | 验证条件 |
| --- | --- | --- |
| 后端完整回归 | 441 passed、1 skipped | 本地 Python 3.12 开发环境；受控用例不算真实服务成功率 |
| 前端默认回归及运行服务检查 | 44 passed | 检查组件、流程、运行 Vite 的实际响应内容 |
| 前端附加真实图布局回归 | 45 passed | 额外读取本轮四任务真实事件，核对四 lane、三入口、一依赖及节点归属 |
| 前端生产构建 | 通过 | TypeScript 检查、Vite 构建 |
| Compose 配置解析 | 通过 | 单实例、可选持久化后端与 HTTPS 配置叠加；未实际启动所有组合 |
| Docker 镜像构建 | 通过 | Python 3.12 / Node.js 22，锁文件，Linux/ARM64 |
| 隔离容器运行 | 15/15 | 密码保护、静态资源、SSE、Chroma、资源路径、非 root、无密钥内嵌、demo 任务 |
| 四任务真实会话 | 4/4 目标、15/15 证据检查 | 本机服务、真实模型与 MCP，不是容器中的 live 测试 |
| 源码发布预检 | 通过 | 白名单导出、已知凭据比对、固定评测输入保留、私有数据排除 |

四任务报告见 [脱敏摘要](assets/showcase-four-tasks-20261026.json)。本次耗时 45.598 秒，独立分支区间真实重叠，路线起点与工具提供并选中的杭州东站坐标一致。模型与工具共同作用，不能以这一个成功会话推断完成率或 P95。

容器报告见 [容器摘要](assets/container-check-20261026.json)。验收使用随机临时展示密码和新匿名卷，不注入模型/地图 Key；检查结束后停止容器并清理匿名卷。报告不包含临时密码或私人对话。

## 复现命令

```bash
# 本地受控回归
uv run --frozen --extra dev --extra music pytest -q

# 前端常规检查；需要先运行对应 Vite 服务
cd frontend
LIFEPLUS_FRONTEND_TEST_URL=http://127.0.0.1:5176 npm test
npm run build
cd ..

# 新的真实四任务会话，会消耗外部服务额度
uv run --frozen --extra dev --extra music python -m evaluations.showcase_smoke \
  --base-url http://127.0.0.1:8000 --output data/showcase/another-run

# 本轮本地私有事件的图布局检查；公开包不含该运行记录
cd frontend
SHOWCASE_GRAPH_REPORT='../data/showcase/readme-four-tasks-20261026/private data/report.json' npm test
cd ..

# 真实容器，但不调用付费模型/地图
docker build -t lifeplus-agent:local .
uv run --frozen --extra dev python scripts/container_smoke.py \
  --image lifeplus-agent:local --output data/container-another-run.json
```

## 未完成与条件限制

- computer use 多次尝试均被浏览器保存的站点权限或管理策略拒绝；用户调整系统权限后重试仍未通过。没有通过 Chrome、CDP 或其他表面绕过，也没有制作冒充页面截图的图片。截图清单见 [showcase.md](showcase.md)。
- 没有创建远端 GitHub 仓库或执行 push；没有实际发布到服务器或申请公开域名证书。
- 没有完成可选 Redis / MySQL / PostgreSQL 的整套容器联测，也没有进行 Linux/AMD64 验收。
- GitHub Actions 配置已加入，但本地验证不是远端 CI 已运行的证明。
- 没有匿名公开访问防滥用系统、多租户 IAM、分布式锁或多副本调度。部署说明限定为受密码保护的单实例、小范围受邀展示。
- 本轮未测生产分布下的任务完成率、并发容量或端到端 P95；README 不沿用无本轮证据的百分比和规模数字。

## 多轮记忆修复追加验收

同日新增会话接续与历史检索修复后，后端完整回归为 **450 passed、1 skipped（23.72 秒）**，其中新增 9 项会话记忆受控测试。前端默认回归及运行服务检查 44 项通过，生产构建通过。

真实配置模型的 HTTP 记忆联测 **7/7 检查通过**，覆盖话题接续、跨会话偏好读取、首次输入原文检索及偏好删除；单次联测总耗时 65.89 秒，不代表单请求延迟或 P95。首次输入检索来自数据库，不计为模型自行回忆成功。当前用户会话也已通过 HTTP 与数据库首条原文逐字比对，公开报告不含私人原文。

说明见 [多轮对话与历史记忆](conversation-memory.md)，脱敏结果见 [记忆联测摘要](assets/memory-live-20261026.json)。上述追加修复未重新构建和验收 Docker 镜像，前文容器结果只对应修复前版本。

## 工业故障契约追加验收

按 [18 问与测试验收手册](LifePlus-Agent-Harness-18问与测试验收手册.md)补充实际测试，而非只增加说明。基线完整回归 450 passed、1 skipped（22.11 秒）；相同 33 个新增契约案例修复前 28 failed、5 passed，修复后 33 passed（0.98 秒）；最终完整回归 **483 passed、1 skipped（22.62 秒）**。XML 和公共案例 ID 可追溯，见 [脱敏摘要](assets/industrial-contracts-20261026.json)。

改进包括：共享计划与路由的槽位契约，禁止未授权字段/绑定目标和重复依赖边；统一 success=false 业务失败和严格布尔标记，矛盾回执失败关闭；MCP 读取 structuredContent 并拒绝非结构化文本。没有放宽原断言，也没有把合法空列表、缺参或真实澄清误判为网络失败。小突发用例验证 36 次提交共 12 个幂等作业、worker 在途上限与最终清理，不是 QPS 压测。

修复后使用隔离本地 HTTP 服务和新数据库进行真实模型/MCP 四任务复验，**4/4 目标、15/15 检查通过，26.453 秒**，见 [摘要](assets/industrial-four-tasks-20261026.json)。验证三独立一依赖、实际并行重叠、选中地点坐标绑定及无业务写入。不与此前单样本做严格性能 A/B，也没有重跑 90 会话完整 live 集。

同一隔离服务的真实模型多轮记忆复验 **7/7 检查通过，总耗时 41.596 秒**，见 [摘要](assets/industrial-memory-20261026.json)。使用新合成用户；归档原文读取是数据库能力，未将其计作模型记忆推理。临时服务、数据库和依赖环境与正常用户会话隔离；没有更改 .env 或真实服务 Key。

本次未重新验收 Docker 镜像、全部浏览器页面、多实例接管、供应商压测或长期稳定性；原始历史记录仍按其版本使用。
