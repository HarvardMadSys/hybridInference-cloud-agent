# 新部署不需要 `.env`

- 日期：2026-11-09
- 依据：[精简 `.env`](2026-11-03-slim-env.zh-CN.md)、[首次安装自动生成密钥](2026-11-04-first-install-keys.zh-CN.md)
- 实现：`apps/backend/cloud_agent/deployment/deployment_keys.py`、`deploy/control-plane/load-keys.sh`、
  `deploy/control-plane/docker-compose.yml` 的 `keys` 服务、`deploy/control-plane/deploy.sh`、
  `apps/frontend/src/components/features/auth/SiteAddressStep.tsx`

## 1. 要解决的问题

#1092 和 #1098 之后，控制面的 `.env` 只剩数据库连接、数据库口令、账号会话密钥和设置根密钥，而且新安装时
由 `deploy.sh` 生成。但 `.env` 这个文件还在：`deploy.sh` 没有它就拒绝运行，Compose 用 `:?` 要求这几个值，
示例文件和文档也还在教人照着填。手动装主机还要手写 27 行。

目标：**新部署不需要手写任何配置文件。** 部署完打开网址，按 `/setup/administrator` 走完，就能开始用。

## 2. 什么不能进数据库

- 数据库连接：没有它就读不到任何设置。
- 设置根密钥：它加密数据库里的所有秘密。放进数据库，数据库备份一泄露就全泄露，所以必须和数据库分开放。

所以这两样仍然在磁盘上，但由系统生成、保管，人不碰。做法不是把它们挪到网页上（那需要一个连数据库之前、
没有登录的 Web 模式，从网页请求写密钥到磁盘，攻击面更大），而是挪进一个 Docker volume。

## 3. 最终形态

### 控制面

```
./deploy.sh  （或 docker compose up）→ 打开网址 → /setup/administrator → Admin → Servers → Add server
```

- **`keys` 服务**：Compose 里一个一次性服务，用控制面镜像，以 root 运行但没有网络、只读根文件系统、
  只保留 `CHOWN` 和 `DAC_READ_SEARCH` 两个 capability。它维护 `agent-keys` volume，每个值一个文件：
  `AGENT_DATABASE_URL`、`AGENT_POSTGRES_PASSWORD`、`AGENT_LOCAL_SESSION_SECRET`、
  `AGENT_SETTINGS_ENCRYPTION_KEY`（老部署可能是 `AGENT_SESSION_SECRET`）。规则：
  1. 环境变量（`.env` 或部署进程）里有值，就存进 volume——已有部署升级后启动一次，就可以删掉 `.env` 里这些行；
  2. volume 里已有的值保留；
  3. 缺的值只有在**能证明是全新安装**时才生成：数据库是自带的，而且它的 volume 里还没有数据库（没有
     `PG_VERSION`）。否则拒绝启动，并说明缺什么、为什么不生成。设置根密钥例外：缺了只警告，因为老部署的
     `AGENT_SESSION_SECRET` 可能从别处提供（沿用 #1098）。
  4. 有了自带数据库的连接串但没有口令时，从连接串里取口令；连接外部数据库时，自带的 Postgres 不服务任何东西，
     随机给它一个口令即可。
- **文件权限**：每个文件 0400，只给读它的用户：数据库口令给 `postgres:16-alpine` 的 uid 70，其余给控制面镜像的
  uid 10001。
- **读取**：控制面镜像的入口脚本 `load-keys.sh` 对每个变量，环境里为空时才从文件读，然后 `exec` 原命令。
  环境里有值就用环境的值，所以已有部署和手动执行的命令行为不变。Postgres 用官方的 `POSTGRES_PASSWORD_FILE`。
- **启动顺序**：`postgres`、`migrate` 都依赖 `keys` 成功退出，`control-plane`、`mcp-relay`、`github-relay`
  经由它们间接依赖。每次 `up` 都会重新运行 `keys`（幂等）。
- **`deploy.sh`**：不再要求 `.env`，有才传 `--env-file`；删除 #1098 的 shell 生成逻辑。
- **备份**：`docker exec <project>-control-plane-1 python -m cloud_agent.deployment.deployment_keys export`
  输出 `.env` 格式的几行，存到加密的备份存储。恢复时把它作为新机器的 `.env`，首次启动就存进 volume。
  不提供网页下载根密钥。数据库口令不在导出里（控制面用户读不到），但自带数据库的连接串里带着它。

### 首次设置

`/setup/administrator` 变成向导：
1. 设置码 + 创建管理员（已有）；
2. 确认站点地址（`web_base_url`，默认填当前页面的地址；已保存的值优先），可以跳过，Admin 顶部的提醒会继续提示；
3. 跳到 Admin → Servers，在那里用 Add server 添加第一台服务器。

没有把 Add server 嵌进向导：全新安装上它要先建并验证 control plane 机器的 SSH 连接、再建自动 server setup，
等于重做半个 Servers 页面。

### 主机

主机的 `.env` 只由 Add server（自动 server setup）生成。删除 `deploy/host/.env.example`，文档不再教手动安装主机。
`deploy/control-plane/compute-host.env.example` 保留：Admin 里的高级手动 setup 和文件式 profile 还在用它。
单元测试 `test_server_setup_writes_every_variable_a_host_cannot_start_without` 保证主机 Compose 要求的每个变量
都由 server setup 写入。

### `*_EXTRA_VERSIONS`

删除。它是构建镜像时多打包几个 CLI 版本的菜单，已被 Admin → Agent versions 的在线下载取代：Claude Code、
Codex、pi、opencode 都能下载；kilo 跑带隔离补丁的构建，本来就只接受评审过的版本；托管主机拉取发布的镜像，
主机上的构建参数也用不上。

## 4. 没做的

- **host capacity、存储探测、profiling 开关挪进 Admin**：示例文件删掉之后，它们只是带默认值的可选覆盖项，没人需要设。
  profiling 的启停已经由 Admin → Measurements 在运行时控制；存储探测在部署时由 shell 读取，要挪需要单独设计
  （精简 `.env` 第 7 节）。
- **staging/prod 的主机**：它们由 freeInference 的部署 workflow 手动维护 `.env`，本次不改。升级前要检查它们
  有没有设 `*_EXTRA_VERSIONS`。
- **网页上改数据库地址或轮换根密钥**：见第 2 节。

## 5. 验证

- `tests/unit/deploy/test_deployment_keys.py`：生成、保留、存储环境值、拒绝、导出与恢复、入口脚本、
  没有 `.env` 时完成部署。
- `tests/unit/test_control_plane_deploy.py`、`tests/unit/test_env_examples.py`：Compose 不再要求这些值、
  每个读取方都等 `keys`、`keys` 的隔离设置、server setup 写全主机必需的变量。
- CI 的 compose job：只有 `AGENT_DEPLOYMENT_SHA` 的 env 文件也能渲染。
- 真实 Docker 演练（独立的 compose project）：全新启动生成 4 个值、权限 70/10001 0400；`migrate` 用 volume 里的
  连接串成功；再次 `up` 不生成；环境里设的值被存入且控制面读到；volume 丢失而数据库还在时拒绝启动。
