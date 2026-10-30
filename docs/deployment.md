# 部署与 GitHub 发布

本文面向受邀面试官的单实例展示。它不是多租户生产平台的上线认证报告；没有替你创建 GitHub 仓库、购买服务器、注册域名或公开端口。

## 1. 最小部署

2026-10-29起配置模板与本机基础LLM采用`deepseek-flash`，通过`MODEL_THINKING=disabled`关闭思考；三个模型角色继承同一配置。仍只启用领域Jev，事件纯回复过滤为独立实验，不随模型切换启用。[配置、实测与回滚说明](deepseek-rollout.md)。真实Key仅写私有`.env`，修改后须重建容器，不能仅restart。

需要 Docker Engine 与 Compose 插件。镜像构建使用 Node.js 22 编译前端，Python 3.12 安装 `uv.lock` 中的基础依赖和 production extra；最终镜像不需要 Vite、Node.js 或开发依赖。音乐检索通过已有 httpx 适配器访问真实 QQ 音乐接口，不安装未使用的 music extra SDK；该 SDK 间接引入的 `urllib3-future` 与基础依赖中的 `urllib3` 在干净容器内发生模块覆盖，曾导致入口导入失败。构建阶段额外导入 API 和 MCP 入口，启动类依赖错误会直接令镜像构建失败。

```bash
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

将生成值填入私有 `.env` 的 `SHOWCASE_PASSWORD`，至少 24 个字符；设置 `SHOWCASE_USERNAME`。同时配置模型端点、Key、实际可用的模型名和高德 Web 服务 Key。不要覆盖你已有的私有 `.env`，已有配置时只补充缺失字段。示例不包含真实 Key。

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
docker compose logs --tail=100 app
```

打开 `http://127.0.0.1:8088`，浏览器会要求输入展示账号与密码。只有 `/healthz` 不要求认证，它仅返回进程可用状态；受保护的 `/api/health` 展示模型和存储配置。无模型 Key 时界面可以启动，但 live 请求失败，不能因此声称真实模型验收通过。

默认数据保存于命名卷，容器使用 UID 10001。使用只读根文件系统和受限 `/tmp`，所有可写业务状态放在 `/app/data`。代码包安装在虚拟环境，`LIFEPLUS_ROOT=/app` 显式定位前端、领域规则和知识目录，不能按 site-packages 路径猜项目根目录。

`docker compose down` 停止服务但保留卷。不要对有价值数据执行 `down -v`；这会删除数据。更新时先备份，再 `docker compose up -d --build`。当前没有数据库版本回滚系统，旧镜像不一定能读取未来修改过的 Schema。

### 本机后台常驻（2026-10-28）

本机使用上述单容器 Compose 配置，访问 `http://127.0.0.1:8088`。账号及随机生成的访问密码保存在本地 `data/local-docker-access.txt`（权限 600），同一份凭据写入私有 `.env`；不要上传这两个文件。镜像不包含真实 Key，运行时由 `.env` 注入，模型选择沿用现有配置。

- 容器以 detached 模式运行，不依赖终端窗口；`restart: unless-stopped` 在容器异常退出、Docker 引擎恢复后自动拉起服务。主动 `stop` / `down` 后不会自动恢复，需再次执行 `docker compose up -d`。
- 本机已安装 `~/Library/LaunchAgents/com.lifeplus.docker-start.plist`，当前用户登录 macOS 时启动 Docker Desktop。这是本机配置，不包含在仓库或服务器部署中；未登录、睡眠、关机或主动退出 Docker 时服务不可用。本次不修改系统睡眠设置。
- 数据保存到 `lifeplus_lifeplus-data` 命名卷，不自动导入开发环境的聊天库。关闭终端或重启容器不会清空数据，`docker compose down -v` 则会删除数据卷。
- 容器健康检查每 30 秒执行一次。`unhealthy` 只标记状态，不会单凭健康失败自动重启；如进程未退出但不响应，应检查日志后手动重启，而不是声明已经具备自动故障修复。
- 默认只绑定本机 loopback 地址，不开放局域网或公网。对外访问仍需按下一节配置 HTTPS、认证和访问边界。

在项目根目录管理服务：

```bash
docker compose ps
docker compose logs --tail=100 app
docker compose restart app
# 暂停 / 恢复（不删除数据卷）
docker compose stop
docker compose up -d
```

若不再需要登录后启动 Docker，在当前用户终端执行：

```bash
launchctl bootout "gui/$(id -u)/com.lifeplus.docker-start"
# 另外移走 ~/Library/LaunchAgents/com.lifeplus.docker-start.plist，避免下次登录再次加载。
```

本机本次验收：完整回归 **563 通过、1 跳过**；隔离镜像验收 **15/15**，见本地 `data/container-check-20261028.json`。常驻容器中的真实模型天气请求完成，SSE 返回正常；另直接验证真实音乐工具返回候选。向空闲应用主进程发送退出信号后，Docker 自动拉起容器，重启次数增至 1，原会话和已完成任务仍可读取。完整摘要保存在本地 `data/local-docker-acceptance-20261028.json`。这不包含整机重启、断电恢复或公网访问测试；一次真实请求的耗时不能作为 P95 或容量结论。

## 2. HTTPS 与面试官访问

1. 准备域名与服务器 DNS 记录，仅开放 80 / 443。模型和地图服务需允许服务器出网。
2. 在私有 `.env` 设置 `LIFEPLUS_DOMAIN=你的域名` 和 `PUBLIC_ORIGIN=https://你的域名`。
3. 启动网关配置：

```bash
docker compose -f docker-compose.yml -f deploy/compose.https.yml config --quiet
docker compose -f docker-compose.yml -f deploy/compose.https.yml up -d --build
```

Caddy 负责证书与 HTTPS，`flush_interval -1` 让 SSE 及时转发，配置语义参照 [Caddy 官方反向代理文档](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#streaming)。网关不配置完整请求日志，应用也关闭普通访问日志，避免把查询参数和 Basic 凭据写进访问日志。不要通过公开链接附带密码，账号密码经单独渠道交付。

此配置给 Caddy 分配专用私有地址 `172.28.90.2`，应用仅信任该代理的转发头；不使用 `forwarded-allow-ips=*`。子网是 `172.28.90.0/24`，如与服务器现有网络冲突，应同时修改 Compose 子网、Caddy 地址与应用的 `FORWARDED_ALLOW_IPS`，不能放宽为任意代理。

默认单机 Compose 显式允许 loopback Host 的本地 HTTP 验收，并只映射宿主机 `127.0.0.1`。HTTPS 配置会关闭该 HTTP 例外；PUBLIC_ORIGIN 校验跨站写入。Host 并不是身份凭据，网络隔离和代理可信范围仍必须保持，不要把本地配置的端口映射改为 `0.0.0.0`。

使用统一展示账号的访客共用一个受保护空间。若需要互不可见的会话，部署不同 Compose 项目名、数据卷与域名，分别配置账号；完整多租户 IAM、审计和配额不是当前项目已完成能力。普通 `APP_MODE=production` 仍拒绝演示身份和未验证 Bearer。

## 3. Redis / MySQL / PostgreSQL

单容器 SQLite 足够进行小规模功能演示。需要验证外部适配器时，在 `.env` 配置三项不同的随机数据库密码，建议使用十六进制字符串避免 DSN 中的 URL 编码问题：

```bash
python3 -c 'import secrets; print(secrets.token_hex(24))'
```

分别填入 `MYSQL_PASSWORD`、`MYSQL_ROOT_PASSWORD`、`POSTGRES_PASSWORD`，再启动：

```bash
docker compose -f docker-compose.yml -f deploy/compose.backends.yml up -d --build
# 同时需要 HTTPS 时再添加 -f deploy/compose.https.yml
```

数据库不映射宿主机端口，应用等待其健康检查。Redis 管短期缓存，MySQL 管记忆，PostgreSQL 管图 Checkpoint；任务事件、工具队列、操作账本和提醒仍使用应用 SQLite 持久卷。**不是所有状态都迁移到了 PostgreSQL。**

这里保持一个应用实例与一个 Uvicorn worker。进程内调度器、锁、SSE 订阅、MCP 会话和提醒调度没有因为配置外部数据库就变成分布式系统，不能直接横向扩容。

## 4. 验收与排障

```bash
# 对真实构建出的镜像做隔离测试：不使用私有模型 Key、不修改当前会话库
uv run --frozen --extra dev python scripts/container_smoke.py \
  --image lifeplus-agent:local --output data/container-check.json
```

验收包括整站认证、SPA 深链接、缺失 API/资源 404、真实镜像中的前端/Skills/知识目录、非 root、无内嵌 `.env`、Chroma 加载、独立 demo 任务与 SSE。它不是模型和高德 live 验收；live 四任务脚本另见 [演示手册](showcase.md)。

2026-10-26 已在本机 Linux/ARM64 容器完成上述 **15/15** 验收，[公开摘要](assets/container-check-20261026.json)记录检查结果。测试容器及其匿名数据卷已清理，未修改现有会话库；没有公开域名证书验收，也没有完成 Linux/AMD64、可选三数据库联合部署和浏览器视觉验收。不能把 Compose 语法检查等同于所有部署组合都运行通过。

| 现象 | 检查 |
| --- | --- |
| 启动失败 | 密码长度、配置文件、数据卷权限、容器日志 |
| 首页 404 但健康检查正常 | 镜像是否包含前端，`LIFEPLUS_ROOT` 是否为 `/app` |
| 401 | 展示账号或密码不正确；不是模型认证失败 |
| 426 | 公网没有经过 HTTPS，或可信代理地址配置错误 |
| 429 | 认证尝试或任务并发过多；检查 Retry-After，不做无限重试 |
| 模型失败 | 核对实际端点与模型名、Key 权限和余额；不要打印 Key |
| 高德错误 | 查看工具错误码与工具耗时；核对接口权限、出网 IP 和额度 |
| SSE 很久不显示 | 区分规划/模型首 Token/工具等待；确认反向代理没有响应缓冲 |
| 同样路线截图缺按钮 | 检查服务器实际返回的前端版本；不要只确认磁盘源码已修改 |

对小规模受邀访问，建议先从 2 CPU / 4 GiB 内存及足够镜像磁盘空间开始验证；这是配置起点，不是已经压测的容量承诺。当前应用容器限制为 2 GiB，带外部数据库时还需额外预留内存。为模型和高德设置供应商预算/配额，临时展示结束后停止站点并轮换展示密码。

## 5. 源码发布包

```bash
uv run --frozen --extra dev python scripts/prepare_release.py --output data/release/lifeplus-agent
```

导出目录和 ZIP 都保存在忽略的 `data/release`，已存在时拒绝覆盖。白名单包括源码、锁文件、前端、测试、`evaluations/data` 固定输入、部署文件及文档；排除真实环境变量、数据库、缓存、原始运行记录、私人会话和日志。不会执行 git add、push 或创建远端仓库。

导出会检查常见 Key 格式以及本地 `.env` 中已知的凭据，但它不能证明历史提交或所有未知格式都不含秘密。发布前复核完整暂存差异、文档和截图，开启 GitHub secret scanning；实际 Key 如果已泄露，应在供应商侧轮换而不是仅从文件删除。

原始 `data/`、`evaluations/runs/` 默认不公开，因此部分历史设计文档中的机器报告链接仅在开发副本有效；不要把链接缺失误当作新版本实测通过。对外证据应放在 `docs/assets`，只保留经审核的公开摘要和截图。尚未选择许可证，请确认原创内容、第三方依赖及服务条款后再选择授权方式。

## 6. 上线前最后检查

- 使用新卷，不能把当前私人聊天库上传到服务器。
- 真实模型与地图在目标服务器分别验收，模型提供商是否允许公开代理访问也需确认。
- 只邀请受信任访客，按访客隔离部署，限制请求额度；不要视为匿名公共 Agent 服务。
- 确认 HTTPS、可信代理、密码保护、SSE、任务取消、超时、写入确认、过期选择与故障提示。
- 备份数据卷，明确日志及会话保留时间；当前没有自动隐私清理服务。
- 检查公开截图是否包含姓名、用户真实位置、账号、完整日志或凭据。

Compose 的依赖健康、只读文件系统、卷和端口等配置参照 [Docker Compose 官方服务规范](https://docs.docker.com/reference/compose-file/services/)。具体限制仍需在目标服务器核验，不把配置声明视为容量实测。
