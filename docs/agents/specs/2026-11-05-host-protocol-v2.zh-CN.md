# host 协议 v2：混合版本测试、版本协商、成员 fence、凭据封装、能力匹配、成员上报和命令队列

日期：2026-11-05。状态：提议，待评审。每一节单独排期，第 3 节是其余各节的前提。

设计核对基线：分支 `murphy/refactor/structural`（PR #1111），提交 `6c930f118`。代码路径相对 `apps/backend/`，行号都是基线上的。

相关：[结构重构](2026-11-05-structural-refactor.zh-CN.md) §10、§13.2、§15、§16；[A2：group 生命周期状态](2026-11-05-group-lifecycle-state.zh-CN.md)；[Agent 自己开 PR](2026-11-04-agent-native-pull-requests.zh-CN.md)；[一个 MCP 概念](2026-11-04-mcp-one-concept.md)；[模型连接](2026-10-27-user-model-connections.zh-CN.md)；[部署可靠性](../../developer/deployment-reliability.md)；[发布](../../../deploy/release/README.md)。

## 1. 结论

- **混合版本测试分两层。** A 层只测线上协议，不用镜像：上一版和本版的代码各跑在自己的 venv 里，在 PR 上跑，约 5 分钟。B 层用真镜像按真实的上线顺序演练一次滚动升级，只在 dev push 和带 `protocol` 标签的 PR 上跑，约 30–45 分钟。今天的发布产物够做 B 层，但有两处缺口：control 镜像只有 arm64，所以上一版的控制面要从 tag 的源码构建；sandbox 只能访问公网，所以 B 层要么用一个真实 provider 的 CI 密钥，要么只验证"失败的一轮"。
- **生产里不存在"混合版本创建 group"。** 新 group 只能建在与控制面同一 SHA 的 host 上（`execution_profiles.py:78`）。混合版本窗口里跑的是已有 group 的后续轮次、心跳、residency、停止，以及控制面对 Sandbox Manager 的直接调用。两层测试都按这个范围设计。
- **协议版本是一个整数，靠 host 心跳协商。** `WorkerClaimRequest` 是 `extra="forbid"`（`schemas/workers.py:91`），老控制面会以 422 拒绝新字段。所以新 host 只有在心跳响应里看到控制面支持 v2 之后，才在 claim 里带版本号。
- **`MemberFence` 只统一形状，不改每个路由要校验哪些分量。** 唯一的语义收紧是给 residency 请求加上 sandbox 和 activation 的代数，单独列为一步，并作为待决问题。
- **凭据封装先做一件不改协议的事。** residency 下发的两个占位凭据（`amr.`、`agh.`）在启动面里已经声明为要脱敏（`cloud_agent_host/launch/mcp_relay.py:43`、`github_relay.py:92`），但没有接进 runner 的脱敏器（`cloud_agent_host/launch/facet.py:58–63`，`turn_child.py:151–152` 只从 claim 取值）。先把这一处接上。线上封装是第二步，要不要做，等第一步完成后再评审。
- **能力匹配挪进 claim 的 SQL，准入一项都不挪。** claim 已经按 `metadata.requires` 匹配 attestation（`storage/repositories/attempt_commands/claims.py:253`），缺的是环境、SSH 和模型连接这几项能力没有算进去。
- **心跳里成员的三个代数和 `resident_state` 必须保留。** 控制面只读每个成员的 `thread_id`、三个代数和 `resident_state`，其余十个字段可以停发。
- **命令队列只装"做不成也没有别的路径兜底"的意图。** 停止和 residency 丢失已经是电平触发的：每次心跳都按数据库状态重新计算并回显，天然幂等。真正没有兜底的是 SSH 访问的撤销：现在是尽力推送，失败就只能等下一轮（`application/ssh_servers.py:441–457`）。所以第一个迁进队列的是 SSH 访问，不是 residency 丢失。

## 2. 现状

### 2.1 线上协议的范围

| 方向 | 通道 | 位置 |
|---|---|---|
| host → 控制面 | 注册、心跳 | `api/hosts.py:171`、`:281` |
| host → 控制面 | claim、等待、attempt 心跳 | `api/worker.py:420`、`:519`、`:550` |
| host → 控制面 | sandbox 转移：reserve、running、residency、process、admit、settle、abandon | `api/worker.py:802`、`876`、`912`、`993`、`1022`、`1051`、`1084` |
| host → 控制面 | 事件、终端挂起和恢复、release、finish 等 | `api/worker.py` 其余路由 |
| 控制面 → host | 心跳响应：设置、archive 策略、`residencies_lost`、`retained_sandboxes_stop` | `schemas/hosts.py:512` 起，`:563`、`:573` |
| 控制面 → host | attempt 心跳响应的 `cancel_requested` | `schemas/workers.py:354` |
| 控制面 → Sandbox Manager | 35 个直接调用 | `sandbox_manager/sandbox_manager_client.py:136` 起 |

### 2.2 生产里的混合版本窗口

- **host 先上线。** 部署时 host 和控制面并行启动；控制面替换线上 API 之前，`deploy_readiness` 要等到每个新对话的放置 host 都以目标 SHA 发过心跳（`cloud_agent/deploy_readiness.py` 模块说明）。在这段时间里，新版 host（N）对着老版控制面（N−1）。
- **托管计算资源上的 host 是例外**，只有新控制面会把它们滚动到新版。在这之前是老 host（N−1）对着新控制面（N）。部署失败回滚时也会出现同样的组合（`docs/developer/deployment-reliability.md` 158、245 行）。
- **建 group 要求同一 SHA。** `shared_sandbox_claim_attested` 要求 attestation 带上与控制面 `AGENT_DEPLOYMENT_SHA` 相同的部署能力（`execution_profiles.py:78–102`，`api/worker.py:465`）。后续轮次只要求 `compatible_shared_sandbox_followup_attested`（`execution_profiles.py:105`），老 host 可以继续服务它已经拥有的 group。

所以两个方向在生产里实际出现的流量都是：注册和心跳，已有 group 的后续 claim，reserve 之后的 sandbox 转移，residency，attempt 心跳和取消，事件，finish，心跳下发的停止和 residency 丢失，以及控制面对 Sandbox Manager 的直接调用。

### 2.3 现有的检查各自覆盖什么

| 检查 | 位置 | 覆盖 | 不覆盖 |
|---|---|---|---|
| 滚动升级 | `scripts/check_rolling_upgrade.py`，由 `ci.yml:703–715` 在 test-db 任务里调用 | 上一版的 `Storage` 在迁移前后通过同一个连接池读写（`tests/support/rolling_api_worker.py`） | 任何 HTTP，任何 host 代码。而且上一版的代码跑在本版的 venv 里（`sys.executable`，`PYTHONPATH=""`），依赖版本也是本版的 |
| 发布验收 | `deploy/release/acceptance.py`，`publish.yml` 的 acceptance 任务 | 用发布产物真实部署控制面，一直走到 host 就绪 fence | 明确不起 host：fence 预期拒绝（模块说明第 19–28 行） |
| Kata 候选检查 | `ci.yml:1821` 的 kata 任务 | 在真 Kata 上构建 runner 和 sandbox 镜像并自检 | 不连控制面，不跑 group |
| 本地全栈 | `deploy/local/` | 一台机器上的完整栈：控制面、Sandbox Manager、runner、sandbox 镜像、MCP relay，普通 Docker | 不在 CI 里跑；不带模型；两边永远是同一版本 |

## 3. 10.0 混合版本测试（前提）

### 3.1 上一版从哪里来

- **tag。** 和滚动升级检查用同一条命令：`git describe --tags --first-parent --match 'agent-*' --abbrev=0 "$base"`（`ci.yml:714`）。`agent-<sha>` 是 `publish.yml:128` 在 dev 上一次 CI 通过的 push 之后打的。
- **镜像。** 发布 `agent-<sha>` 带两个附件：`release.json` 和 `recipe.tar.gz`（`publish.yml:637–638`）。`release.json` 按 digest 列出六个镜像，平台见 `deploy/release/artifact_catalog.json`：
  - control（linux/arm64）、web-root（linux/arm64）在 `ghcr.io/harvardmadsys/cloud-agent`；
  - runner（linux/amd64）、tunnel（linux/amd64）也在这个仓库；
  - sandbox（linux/amd64）在 `ghcr.io/harvardmadsys/cloud-agent-sandbox`。

  用 `gh release download "$tag" -p release.json` 取出 digest，CI 任务以 `packages: read` 登录 GHCR 拉取，做法和 publish 的 acceptance 任务一样。
- **源码。** `git archive "$tag"`，和 `check_rolling_upgrade.py` 一样。新的检查要用上一版自己的依赖：在归档出来的目录里执行 `uv sync --frozen`，生成单独的 venv，并使用 CI 的 uv 缓存。滚动升级检查现在用的是本版的 venv，这也是一个缺口；可以顺手改，但不在本文范围内。

### 3.2 A 层：线上协议对照，不用镜像

**放在哪里。** 新脚本 `scripts/check_mixed_protocol.py`，在 test-db 任务（`ci.yml:544`）里紧接滚动升级检查执行，用同一个 Postgres 服务，只在 shard 1 上跑。只要改动涉及下列任何一处，`changes` 任务就把它标成需要跑：`schemas/workers.py`、`schemas/hosts.py`、`api/worker.py`、`api/hosts.py`、`cloud_agent_shared/`、`cloud_agent_host/control_plane_client.py`、`cloud_agent_host/service/enrollment.py`、`cloud_agent_host/claimed_job.py`、`sandbox_manager/`、`cloud_agent_host/manager_api/`。

**做法。** 两个方向各起一个随机数据库，用对应控制面那一版自己的 Alembic 迁移：

- **方向 1：本版 host 对上一版控制面。**
  1. 用上一版的 venv，在回环地址上以 uvicorn 起上一版的控制面 app，不设 `AGENT_DEPLOYMENT_SHA`，这样建 group 不受同一 SHA 的限制。A 层测的是线上形状，不是放置策略。
  2. 用上一版的 `Storage` 写入夹具：一个账户、一条模型连接（直接写行，跳过验证）、一个临时工作区对话、一个 host 注册令牌。写法和 `rolling_api_worker.py` 一样：调的是上一版的存储 API，签名变了，就要在这里保留旧写法，直到新写法发布。
  3. 用本版的 host 端代码，也就是 `cloud_agent_host.service.enrollment` 的注册和心跳，以及 `control_plane_client` 的 claim、attempt 心跳、sandbox 转移、residency、事件和 finish，按 §3.4 的脚本走一遍。Sandbox Manager 换成一个按脚本应答的假实现。
- **方向 2：上一版 host 对本版控制面。** 本版控制面在本版 venv 里起；上一版的 host 端代码在上一版的 venv 里，由本仓库的驱动脚本 `tests/support/mixed_protocol_host.py` 调用。这个驱动脚本只调用上一版存在的 API，和 `rolling_api_worker.py` 是同一种约定。
- **Sandbox Manager 的线上协议**单独对照一次：一边是本版的 `SandboxManagerClient`，另一边是上一版的 `manager_api` app（后端是假的，用上一版 `tests/` 里的 fake）；反过来再跑一次。只覆盖 §2.1 表里那 35 个方法中、§9.1 分类为"命令"或"状态"的那些，交互式调用不做。

**断言。** 每一步都检查：接收方用它自己的模型解析成功（不出现 422，也不出现被静默丢掉的字段；为此在接收端打开 Pydantic 的"未知字段"告警计数）；响应码符合预期；最后数据库里的状态（attempt 终态、成员的三个代数、`resident_state`、residency 是否关闭）和同版本配对跑出来的结果一致。同版本配对的结果作为基准，每次一起跑。

**成本。** 建两个 venv：有缓存时约 30–60 秒，冷缓存约 3 分钟。两个方向的脚本各约 1 分钟。总计约 3–5 分钟，比现在的 dbtest 分片短。

### 3.3 B 层：滚动升级演练，用真镜像

**放在哪里。** 新任务 `mixed-version`，跑在 `[self-hosted, Linux, X64, image-verify-x64]` 上，和 kata 任务一样只接受评审过的代码：dev 上的 push，加上带 `protocol` 标签、由维护者触发的 `workflow_dispatch`。`concurrency` 组和 kata 任务共用，避免两个任务同时占用这台机器。

**栈。** 以 `deploy/local/docker-compose.yml` 为底：两个版本的树里都有这份文件。另写一个 override 文件，把镜像换成指定版本：

- 上一版的 runner、Sandbox Manager（同一个 runner 镜像）、sandbox、tunnel：从 `release.json` 按 digest 拉取。
- 上一版的控制面：control 镜像只有 arm64，所以在 x64 机器上用归档出来的 `deploy/control-plane/Dockerfile` 构建，或者直接用上一版的源码起进程。
- 本版的镜像：runner 和 sandbox 和 kata 任务一样在本机构建（`ci.yml:1854–1855`），control 和 tunnel 用各自的 Dockerfile 构建。

用普通 Docker 隔离，这是可信 CI 夹具仓库的场景（AGENTS.md §7：普通 Docker 只用于可信仓库）。

**模型。** sandbox 只能访问公网 IPv4 的 80/443（AGENTS.md §7），而且没有任何开关能放宽，所以本机的假 provider 访问不到。有两种选择（§12 Q1）：

- **(a)** 用一个真实 provider 的 key 作为 CI 密钥，只在 dev 和维护者触发时可用。轮次能完整跑完，可以覆盖 settle 和原生会话续接。
- **(b)** 模型连接的行直接写进数据库，Base URL 指向一个公网上必然拒绝连接的地址。轮次会以确定的方式失败，覆盖 claim、租约、residency、abandon、finish 和停止，但覆盖不到 settle 和续接。

建议 (a)，不行再退到 (b)。

**演练脚本**，按生产顺序：

- **序列 A：先升 host。**
  1. 上一版的控制面和上一版的 host，新建一个对话，跑第 1 轮。这一步会建 group，两边版本相同。
  2. 只把 runner 和 Sandbox Manager 服务换成本版，保留 group。
  3. 第 2 轮：后续轮次，本版 host 对上一版控制面。
  4. 等一次空闲停止，它由心跳下发 `retained_sandboxes_stop`。
  5. 把控制面换成本版，执行迁移。
  6. 第 3 轮续接；再新建一个对话。
- **序列 B：先升控制面。** 托管计算资源上的 host 就是这个顺序。
  1. 两边都是上一版，跑第 1 轮。
  2. 把控制面换成本版。
  3. 第 2 轮：上一版 host 对本版控制面。
  4. 从管理接口请求一次停止，再让模型连接的 key 换代，触发 residency 丢失。
  5. 把 host 换成本版，跑第 3 轮。

每一步断言：轮次终态、事件序列里没有协议错误、成员三个代数单调、心跳下发的停止和 residency 丢失都被执行。

**成本。**

| 步骤 | 时间 |
|---|---|
| 拉 sandbox 镜像（五个 CLI，几个 GB），没缓存 | 3–6 分钟 |
| 构建本版镜像 | 5–10 分钟 |
| 构建上一版 control 镜像 | 3 分钟 |
| 起栈和换服务，共四次 | 每次约 2 分钟 |
| 两个序列的六轮，用 (a) | 每轮 1–2 分钟 |

两个序列串行，总计约 30–45 分钟。Kata 机器已经在为每个 dev 提交构建这两个镜像，可以共用缓存。

**它不覆盖的东西：** Kata 本身、真实的 SSH 隧道拓扑（本地栈的 relay 没有 SSH）、多 host 迁移。这些仍由 kata 任务和 staging 验收负责。

### 3.4 两层共用的演练步骤

| # | 步骤 | 涉及的线上内容 |
|---|---|---|
| 1 | 注册、心跳 | `HostRegisterRequest`、`HostHeartbeatRequest/Response` |
| 2 | claim（新对话，同版本时）/ 后续轮次（混合时） | `WorkerClaimRequest/Response`、`WorkerThreadSandbox` |
| 3 | reserve → running → residency → process → admit | §5 的全部 sandbox 请求 |
| 4 | 事件、attempt 心跳；中途请求取消，看到 `cancel_requested` | `WorkerHeartbeatResponse` |
| 5 | settle 或 abandon，然后 finish | `WorkerSandboxSettleRequest` 等 |
| 6 | host 心跳带 `retained_sandboxes`；控制面下发停止；下一次心跳报告已停止 | `RetainedSandboxBeat`、`RetainedSandboxStop` |
| 7 | 让 residency 失效，看心跳响应的 `residencies_lost` 和 host 的执行 | `ResidencyBeat` |
| 8 | 控制面直接调 Sandbox Manager：状态、SSH 同步、终端挂起和恢复 | `sandbox_manager_client.py` |

### 3.5 交付和验收

1. 第一个 PR 只加 A 层，不改协议。合并前在 dev 上连续跑绿一周，同时人为制造一次不兼容（例如删掉一个老 host 会发的字段），确认它会失败。
2. 第二个 PR 加 B 层。
3. §4–§9 的每个协议 PR，两层都要在那个 PR 上跑绿，并把结果写进 PR 描述。`check_rolling_upgrade.py` 仍然要通过。

**A 层的现状（2026-11-05）。** 已实现，尚未在 dev 上连续跑满一周：

- 脚本 `scripts/check_mixed_protocol.py`，两个进程驱动 `tests/support/mixed_protocol_control.py`（某一版的控制面）和 `tests/support/mixed_protocol_host.py`（某一版的 host 端代码），共用的模型检查在 `tests/support/mixed_protocol_wire.py`。上一版用 `git archive` 取出，在自己的目录里 `uv sync --frozen` 建 venv（含 dev 依赖：Sandbox Manager 配对要用那一版 tests 里的 fake）。
- 四组配对并行跑：本版 host 对上一版控制面、上一版 host 对本版控制面，以及两版各自同版本配对作为基准。每组一个随机数据库，用该组控制面自己的 Alembic 迁移。控制面是真实的 `create_app()`（含后台任务），出站 HTTP 指向一个关闭的代理端口。
- 覆盖 §3.4 的 1–7 步：注册和心跳（`retained_sandboxes` 由那一版自己的心跳行构建函数从假 Sandbox Manager 读出）；新对话 claim 和两次后续 claim；reserve、running、residency、process、admit；事件和 attempt 心跳，第二轮中途取消并在心跳里看到 `cancel_requested`；settle 和 finish，第三轮 abandon 和 finish；心跳下发空闲停止、host 停掉后下一次心跳报告已停止；驱逐 residency 后心跳返回 `residencies_lost`。
- 断言：每次调用的状态码；接收方用自己那一版的模型解析——控制面按路由的请求模型、host 按路由的响应模型、假 Sandbox Manager 按 host 那一版的 `manager_api` 请求模型——出现被拒收或被静默丢弃的非空字段就失败；数据库状态在第一轮之后、驱逐之后、空闲停止之后和结束时，都要和同版本基准一致。已知的丢弃只有一项白名单：控制面在终端挂起和恢复里发的 `lease_generation`，由 `sandbox_manager_client.py` 的 `COMPAT(remove-after=2026-11-30)` 管着。
- CI：`ci.yml` 的 test-db 任务在滚动升级检查之后、只在 shard 1 上跑，由 `changes` 任务的 `protocol` 输出控制（路径见 §3.2，另加 `api/worker_events.py`、`service/host_control.py`、`service/host_service.py`、`cloud_agent_host/sandbox_manager_client.py` 和检查自己的文件）。本地对 `agent-9a2b9e1` 跑一次约 10 秒。
- 人为不兼容已验证会失败：把 settle 的 `native_session_id` 改名，两个混合方向都报"控制面的请求模型丢弃该字段"和数据库差异，基准两组通过；把心跳响应的 `retained_sandboxes_stop` 改名，两个混合方向都报 host 的响应模型丢弃该字段、没收到停止，以及空闲停止之后的数据库差异。
- Sandbox Manager 线上协议（§3.2 第三条）：另跑四组配对（`tests/support/mixed_protocol_manager.py`）——本版控制面的 `SandboxManagerClient` 对上一版的 Sandbox Manager app、上一版的 client 对本版的 app，以及两版各自同版本配对。app 后面是那一版自己 `tests/unit/test_agent_sandbox_manager.py` 里的假 supervisor（`_SharedSupervisor`，补上它测试里没用到的 `residency_lost`）。覆盖 §9.1 里算作命令或状态的调用：`health`、`list_sandboxes`（全部和按 thread）、`shared_chat_sandbox_status`、`register_shared_sandbox`、`shared_residency_lost`、`sync_ssh_access`、`suspend_terminals`、`resume_terminals`、`resume_settled_terminals`。断言：两端都不报错、无 4xx、Sandbox Manager 的请求模型没有丢弃非空字段（`lease_generation` 同上白名单）、应答不缺 client 自己那一版 Sandbox Manager 会给的键（应答是普通 JSON，client 没有模型）。人为把 residency-lost 的 `generation` 改名，两个混合方向都报 422 和字段丢弃，基准两组通过。
- §3.4 第 8 步：host 流程在 running 之后调 `suspend_terminals("workspace_preparing")` 和 `resume_terminals()`，控制面再直接调 Sandbox Manager 的 suspend/resume，由假 Sandbox Manager 按 host 那一版的模型检查；finish 时控制面对 Sandbox Manager 的 `register` 和终端 resume 同样被检查。状态、SSH 同步和 residency 丢失走上面的 Sandbox Manager 配对。
- 未做：删除和 archive（`admin-delete/*`）的五个调用——它们有自己的持久状态机，请求体由 `workers/sandbox_deletion.py` 按数据库行构建，假 supervisor 后面也没有导出和上传，手写请求体测的就不是控制面真实发出的东西；环境准备和状态；交互式调用（文件、终端 I/O、git、问答、Fork、mid-turn 输入）。控制面主动推 SSH 访问（`push_ssh_access`）要求 owner 有 SSH 服务器，A 层没有建，只对照了线上形状。本地全套（八组）约 10–14 秒。
- 顺带：host 心跳里一直发送的 `"profiling": null` 已删除——没有调用方传值，本版和上一版控制面的 `HostHeartbeatRequest` 都没有这个字段。host 仍读心跳应答里的 `profiling`，两版控制面的 `HostHeartbeatResponse` 也都没有这个字段，未改动。

## 4. 10.1 协议版本

### 4.1 线上改动

- `cloud_agent_shared/host_protocol.py` 定义 `HOST_PROTOCOL_VERSION: Final[int]`，以及控制面用的 `HOST_PROTOCOL_MIN: Final[int]`。今天的协议记为 1，这次改动本身记为 2。
- host 在 `HostRegisterRequest` 和 `HostHeartbeatRequest` 里带 `protocol_version: int`。这两个模型都没有 `extra="forbid"`，老控制面会忽略这个字段。
- 控制面在 `HostRegisterResponse` 和 `HostHeartbeatResponse` 里返回 `protocol: {"min": int, "max": int}`。老 host 会忽略它。
- host 在本地算出生效版本 `v = min(host_max, control_max)`。如果 `v < max(host_min, control_min)`，host 不再 claim，并在下一次心跳的 `launch_error` 里报告 `protocol_unsupported`。
- **claim 只在协商之后才带版本号。** `WorkerClaimRequest` 是 `extra="forbid"`。host 只有在最近一次心跳响应里看到 `protocol.max ≥ 2` 时，才在 claim 里带 `protocol_version`。控制面把没有这个字段的 claim 当作 v1。
- 控制面按每个请求自己声明的版本决定响应形状。控制面主动下发的东西（心跳响应、命令）按这个 host 最近一次心跳的版本。
- 心跳版本不在 `[HOST_PROTOCOL_MIN, HOST_PROTOCOL_VERSION]` 里的 host，控制面把它置为 draining，原因记为 `protocol_unsupported`。它发来的 claim 返回 409，错误类型 `host_protocol_unsupported`。

**版本号和能力字符串分开。** 线上形状的变化（字段、校验、语义）升版本号；host 能做什么（Kata、restore、SSH、环境）仍用能力字符串。§5–§9 每做完一项升一次版本号。

### 4.2 N/N−1

- `HOST_PROTOCOL_MIN` 等于上一个发布的 `HOST_PROTOCOL_VERSION`，而不是"本版减一"：一个发布里可能升了两次版本号。A 层测试从归档出来的上一版里读出它的 `HOST_PROTOCOL_VERSION`，断言本版的 `HOST_PROTOCOL_MIN` 不大于它。
- 兼容代码只放在两个模块里：`cloud_agent/api/worker_compat.py`（处理老 host 的请求）和 `cloud_agent_host/control_plane_compat.py`（处理老控制面的响应）。每个分支都标 `COMPAT(remove-after=…)`。现有的十处 COMPAT 标记和那些没有标记的"老版本不带这个字段"写法（例如 `control_plane_client.py:238` 的事件批量回退、`/worker/wait` 返回 404 时的轮询回退），碰到相应文件时迁进这两个模块。

### 4.3 存储

`agent_runner_hosts` 加两个可空列：`protocol_version int`（来自最近一次心跳）和 `protocol_error text`。只用于管理界面和命令下发，claim 不读它。迁移是纯加列，上一版可以忽略。

### 4.4 上线顺序

1. host 开始发 `protocol_version`，并解析响应里的 `protocol`（没有就当 v1）。
2. 控制面开始返回 `protocol`，存版本号，接受 claim 里的 `protocol_version`。
3. 下一个发布才开始对不在范围内的 host 做 drain。这样不会把还没升级的 v1 host 误判出去：那时 v1 已经是 `HOST_PROTOCOL_MIN`。

### 4.5 测试

- 单元测试：生效版本的计算，以及三种失败（host 太老、控制面太老、没有交集）。
- 契约测试：claim 带不带 `protocol_version`，在 v1 和 v2 控制面上的结果。
- A 层：两个方向都要出现"v1 对 v2"的协商。

### 4.6 不在范围内

不用版本号代替能力字符串；不做按功能的版本协商；不给 Sandbox Manager 的线上协议单独编号，它随 host 一起发布，用同一个版本号。

## 5. 10.2 `MemberFence`

### 5.1 现状

| 路由 | 现在带的 fence | 位置 |
|---|---|---|
| `sandbox/reserve` | attempt 租约，加上不可变的身份字段 | `schemas/workers.py:395` |
| `sandbox/running` | `sandbox_generation`、`activation_generation` | `schemas/workers.py:417` |
| `sandbox/residency` | 只有 `sandbox_id`；`runtime_id`、`runtime_version` 收下后直接丢掉（`storage/repositories/residency.py:67`） | `schemas/workers.py:426` |
| `sandbox/process` | sandbox、activation、residency、process | `schemas/workers.py:434` |
| `sandbox/admit` | sandbox、activation、residency | `schemas/workers.py:443` |
| `sandbox/settle` | sandbox、activation、residency | `schemas/workers.py:451` |
| `sandbox/abandon` | sandbox、activation | `schemas/workers.py:471` |

所有路由都另外带 attempt 租约：URL 里的 `job_id`，加上 attempt 作用域的 `worker_token`。

### 5.2 线上改动

```python
class MemberFence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sandbox_id: str = Field(..., min_length=1, max_length=256)
    sandbox_generation: int = Field(..., ge=1)
    activation_generation: int = Field(..., ge=1)
    residency_generation: int | None = Field(None, ge=1)
    process_generation: int | None = Field(None, ge=1)
```

- 放在 `cloud_agent_shared/sandbox_manager_wire.py` 旁边的新模块 `cloud_agent_shared/member_fence.py`，host 和控制面共用。
- 每个路由在服务端声明自己要求哪些分量：`running` 和 `abandon` 要求 s、a；`admit` 和 `settle` 要求 s、a、r；`process` 要求 s、a、r、p。少了要求的分量返回 422；**多带了不要求的分量也返回 422**，免得"带了却不校验"造成误解。每个路由校验的集合和今天完全一样。
- 校验函数放进 `storage/_sql/fences.py`，和结构重构 §7.4 的 `lock_live_attempt` 放在一起。现在散在各个 repository 里的比较改成调用它。
- **residency 的收紧单独做一步**，作为下一次升版本：要求 s、a，让 residency 不再只靠 attempt 租约。这是语义收紧，要先确认"建 residency 时 activation 一定已经确定"（§12 Q3）。
- `runtime_id` 和 `runtime_version` 在 v2 的 residency 请求里删掉。

### 5.3 N/N−1

- 本版控制面两种形状都接受：平铺字段（v1）或 `fence`（v2）。两种都带而且不一致时返回 422。`worker_compat.py` 把平铺字段转换成 `MemberFence`。
- 本版 host 在协商版本 ≥ 3 时只发 `fence`，否则只发平铺字段。老控制面要求平铺字段，而且会忽略多余的 `fence`，但两种都发就会出现上面"不一致"的风险，所以不两种都发。
- residency 请求的 `runtime_id` 在 v1 里是必填，所以对老控制面继续发送。

### 5.4 存储

不需要迁移。

### 5.5 上线顺序

1. 控制面接受 `fence`（并且仍然接受平铺字段）。
2. host 在协商到 v3 后改发 `fence`。
3. 下一个发布删除平铺字段的兼容分支。

这和"host 先上线"并不冲突：host 在老控制面上协商不到 v3，就继续发平铺字段。

### 5.6 测试

- 表驱动：每个路由 × {缺少每个要求的分量, 多带每个不要求的分量, 每个分量过期一代} → 和今天相同的拒绝。
- 等价性：同一组夹具分别用平铺字段和 `fence` 发送，数据库结果完全相同。
- A 层两个方向都要跑。

### 5.7 不在范围内

不把这些路由合并成一个 `POST /sandbox/transition`（结构重构 §10.2 的决定）；不改各路由校验哪些分量（residency 那一步除外）；不改 attempt 租约。

## 6. 10.3 凭据封装

### 6.1 清单

| 凭据 | 线上位置 | 用的人 | 进 sandbox 吗 | runner 脱敏吗 |
|---|---|---|---|---|
| host 凭据 | `HostRegisterResponse.credential`（`schemas/hosts.py:151`） | host | 否 | 不在本文范围 |
| `worker_token` | claim（`schemas/workers.py:310`） | runner | 否 | 否 |
| `mcp_token` | claim（311），弃用，2026-11-30 到期 | 没人用 | 否 | 否 |
| `clone_token` | claim（320） | runner 克隆 | 否 | 否 |
| `model_connection.api_key` | claim（`cloud_agent_shared/model_connections.py:201`，标记 `MASKED`） | 成员 CLI，或只给翻译器 | 是 | 是，经 `secrets()`（280） |
| `model_connection.local_token` | claim（210，`MASKED`） | 成员 CLI，翻译方案 | 是 | 是 |
| SSH 私钥 | claim 的 `ssh_access`（`cloud_agent_shared/ssh_access.py:141`，`MASKED`） | 成员的私有 tmpfs | 是 | 是（243） |
| 环境 Secrets | claim 的 `environment`（`cloud_agent_shared/environment.py:271` 的 `secrets()`；243 行 `previous_secrets` 标记 `MASKED`） | 成员环境 | 是 | 是 |
| `SandboxMcpServer.secret_env` | claim（`cloud_agent_shared/sandbox_mcp.py:81`，`MASKED`） | slot runner | 是 | 是，但短于 8 个字符（`MIN_SECRET_CHARS`，`secret_redaction.py:15`）的值不遮（`sandbox_mcp.py:131–144`） |
| owner key（`credential`） | residency（`schemas/hosts.py:469`） | 成员 CLI | 是 | 值和 claim 那份相同，所以实际上被遮了 |
| `mcp_credential`（`amr.`） | residency（479） | 成员 CLI，经 MCP relay | 是 | **否**：`McpRelayFacet.secrets()` 声明了它，但没有接进脱敏器 |
| `github_credential`（`agh.`） | residency（487） | 成员的 git 和 gh，以 `GH_TOKEN` 注入（`cloud_agent_host/launch/github_relay.py:73`），经 GitHub relay | 是 | **否**：同上（92） |

runner 的脱敏器在每轮开始时用 `claim_secret_values(job)` 构造（`cloud_agent_host/turn_child.py:151–152`）。这个函数按字段上的 `MASKED` 标记推出脱敏集合（`claimed_job.py:206–221`，由 `tests/unit/test_claim_secret_masking.py` 保证），这就是结构重构里的 B1，基线上已经完成。residency 是在这之后、启动成员时才拿到的（`control_plane_client.py:499`，`shared_live_attempt.py:947`），它的值从来没有加进去过。`launch/facet.py:58–63` 的文档字符串也写着"not yet wired into it"。

结构重构 §10.3 表里说 `secret_env` "否"，这一点在 #1104 里已经改了（`c30d7dfa2` 又补上了短值的规则）。两个占位凭据的"否"现在仍然成立。

### 6.2 第一步：不改协议，把 residency 的值接进脱敏器

- `ControlPlane`（`cloud_agent_host/control_plane_client.py:81`）加一个方法 `add_secrets(values)`：它往 `_secret_values` 里追加，然后重建 `_redactor`。已经打开的流式预览，按现有的 reset 规则重建（`control_plane_client.py:182–184`）。
- `shared_live_attempt` 拿到 residency、构造出 `ResidentLaunch` 之后，立刻用 `launch_secrets(launch)` 调用它。`launch_secrets` 合并所有 facet 的 `secrets()`，取代 facet.py 文档里说的"另一半"。
- 测试：一个 residency 带 `amr.`/`agh.` 值的夹具，成员把它们打印到输出里，断言持久化的事件里只剩 `[secret:mcp_credential]` 和 `[secret:github_credential]`。另加一个反射测试：每个 `LaunchFacet` 子类里声明为 `Secret` 的字段，都要出现在 `secrets()` 的返回值里（结构重构 §6.2 的防漏测试，扩展到 residency）。
- 这一步把"占位凭据要不要脱敏"（结构重构 §15）定为**脱敏**：成本为零，又能防止 residency 还活着时凭据从事件日志里被别人看到。要由写两个 relay 的人在评审时确认。

### 6.3 第二步：线上封装（第一步完成后再评审）

B1 已经让"漏掉脱敏"在 claim 上不可能静默发生，第一步又补上了 residency，所以封装剩下的价值只有两点：线上标明每个凭据给谁用；以及以后新加凭据时只有一个地方可放。要不要做，在第一步完成后再评审。如果做：

```python
class DeliveredCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str        # 本响应内唯一，也是脱敏标记里的名字，例如 "inference_credential"、
                   # "compat_token"、"ssh:<server_id>"、"env:<NAME>"、
                   # "sandbox-mcp:<alias>:<NAME>"、"mcp_credential"、"github_credential"
    kind: Literal["worker", "clone", "model_key", "translator_token", "ssh_key",
                  "env_secret", "sandbox_mcp_secret", "mcp_relay", "github_relay"]
    audience: Literal["runner", "member", "slot_runner"]
    value: SecretStr
    revision: int | None = None
    expires_at: datetime | None = None
```

- claim 和 residency 各带一个 `credentials: list[DeliveredCredential]`。原来放值的地方改成引用：`ModelConnectionAccess.api_key_ref`、`SshServerAccess.private_key_ref`、`BoundEnvironment` 的 secret 引用、`SandboxMcpServer.secret_env` 的值，以及 residency 的 `credential_ref`、`mcp_credential_ref`、`github_credential_ref`。
- **`mask` 不上线。** 要不要遮由 host 按 `kind` 查自己的表决定，控制面不能把它关掉。表里每个 `kind` 都遮，`sandbox_mcp_secret` 保留短值规则。有一个测试保证每个 `kind` 在表里都有一行。
- `id` 就是今天脱敏标记里的名字。有一个测试断言，同一个夹具在封装前后，持久化输出完全相同。
- 投递时机不变：SSH 密钥和环境 Secrets 仍然只随本轮的 claim 下发。owner key 仍然下发两次，等结构重构 §15 的问题回答之后再说。

**N/N−1。** 控制面只对协商到这个版本的 claim 和 residency 发 `credentials` 加引用，对老 host 继续发平铺字段，同一个值不发两份。host 端由 `control_plane_compat.py` 把平铺字段转成封装形式，之后只有一条代码路径。

**存储。** 没有：所有凭据都是每次 claim 时签发或解密的。

**上线顺序。** host 先能读两种形状，然后控制面对新 host 改发封装形式，最后删掉平铺字段。

**测试。** A 层两个方向都要跑；B 层断言 sandbox 里拿到的环境变量、tmpfs 文件和今天逐字节相同。

### 6.4 不在范围内

不改凭据的格式和前缀（`tokens.py`）；不改 relay 的鉴权；不改 host 凭据和 Sandbox Manager 的 pool 凭据；不删掉 claim 里的 owner key（结构重构 §15）。

## 7. 10.5 能力匹配和准入

### 7.1 现状

- claim 的 SQL 已经要求 `COALESCE(j.metadata->'requires', '[]') <@ 本次 attestation 的能力`（`storage/repositories/attempt_commands/claims.py:253`）。但现在唯一写 `requires` 的是 goal 轮次的 `GOAL_REPORT_PROTOCOL`（`storage/repositories/conversation_commands.py:56–61`）。
- 环境、环境推理名、SSH、模型连接这几项能力，是 claim 之后在 `application/turn_claim.py` 里检查的（`:467–469`、`:523`、`:556`）。不满足就 `release_claim(retry_after_seconds=30)`（`:97`、`:270`、`:344`）。

### 7.2 改动

- **按当前状态算，不按入队时算。** 轮次在队列里等着的时候，owner 可能给对话加上环境绑定，或者打开一个 SSH 服务器。所以不能在入队时把能力写死进 `requires`，而是在 claim 的 SQL 里，按这一轮的对话当前的状态，算出它需要的能力集合：
  - 有环境绑定：`ENVIRONMENT_CAPABILITY`；
  - 绑定里用到推理名：`ENVIRONMENT_INFERENCE_CAPABILITY`；
  - 有启用的 SSH 服务器：`SSH_ACCESS_CAPABILITY`；
  - 有模型连接：`MODEL_CONNECTION_CAPABILITY`。

  "用到推理名"在 SQL 里算不出来，所以在环境绑定保存时多存一个派生列 `required_capabilities`。保存是唯一的写入方，结果是确定的。
- **claim 之后的能力检查保留，作为兜底。** 它改成只记一条 `claim_capability_mismatch` 指标和日志，然后照旧 release。正常情况下这个计数应该是零。
- 能力字符串统一成 kebab-case：现在 `GOAL_REPORT_CAPABILITY = "goal_report_v1"`（`cloud_agent_shared/shared_protocol.py:18`）和其他的写法不一致。保留一个版本的别名，两种写法都匹配。
- **准入一项都不挪。** owner 的权限、仓库授权、环境绑定的 access generation、团队成员资格、模型连接的 revision 和 plan、SSH 服务器是否仍然启用，仍然在 claim 时、在 `hand_over_claim`（`application/turn_claim.py:153`）里逐项读取。AGENTS.md §7 要求每一次把值交给容器的读取都重新检查准入，提前到入队时或者写进 SQL 的匹配里，都会漏掉撤销。

### 7.3 N/N−1

能力字符串由 host 的 attestation 报告，已经是逐项容忍的：老 host 不报某项能力，就领不到需要它的轮次。kebab-case 的别名放进 `worker_compat.py`。

### 7.4 存储

环境绑定表加一个派生列 `required_capabilities`，由一次迁移回填。迁移是加列，上一版可以忽略；上一版写入的绑定，在第一次被本版读到时补算。

### 7.5 上线顺序

只改控制面，不依赖 host 的版本。

### 7.6 测试

- claim SQL 的表驱动：对话状态 × host 能力 → 领得到或领不到。
- 兜底的计数为零：在 `tests/servers` 里跑全部 claim 用例，断言 `claim_capability_mismatch` 一次也没有出现。
- 撤销仍然有效：入队后、claim 前，撤销团队成员资格或者 SSH 服务器，claim 照旧拒绝或去掉对应的值。

### 7.7 不在范围内

准入的任何改动；host 行上的能力（只用于管理界面和容量估算）；固定在某个 host 上的后续轮次，如果那台 host 缺能力就永远领不到——现在是每 30 秒领一次再还回去，以后会变成一直等待，要在界面上显示原因（§12 Q5）。

## 8. 10.6 成员状态上报

### 8.1 心跳

`RetainedSandboxBeat.members`（`schemas/hosts.py:209`）用的是 `WorkerSandboxMember`（`schemas/workers.py:142`），一共 15 个字段。控制面实际读取的只有：

- `thread_id`、`activation_generation`、`process_generation`、`residency_generation`、`resident_state`：`reported_members`（`storage/_sql/sandbox_observations.py:114`）取的就是这五个，`_REPORTED_MEMBERS_SQL`（83）用它们更新成员，`_member_moves`（162）用它们判断要不要写；
- `residency_generation`、`resident_state`：续租 residency（`api/hosts.py:356–362`）；
- `residency_generation`、`process_generation`、`resident_state`：释放空闲的 residency（`api/hosts.py:479–505`）。

另外十个字段，`native_session_id`、`native_state`、`context_status`、`resident_slot_id`、`active_attempt_id`、`admitted_job_id`、`admitted_at`、`last_settled_job_id`、`forked_from_thread_id`、`fork_anchor_job_id`，心跳路径上没有人读。`_sandbox_observation`（`api/hosts.py:422–431`）会把它们整个 dump 进去，但下游只经过 `reported_members`。

**代际字段必须保留。** group 级的 `generation`、`container_id` 由 `_fence_refuses`（149）用来拒收迟到的报告；成员级的三个代数由 `_member_moves`、`_REPORTED_PROCESS_GENERATION` 和 `_REPORTED_RESIDENT_STATE`（67–78）用来判断这份报告是否比数据库里的新。去掉它们，一份迟到的旧报告就可能覆盖新进程的状态。

**线上改动。** 新模型 `ReportedMember(thread_id, activation_generation, process_generation, residency_generation, resident_state)`，`extra="ignore"`。v≥k 的 host 只发这五个字段。控制面的 `RetainedSandboxBeat.members` 改成 `list[ReportedMember]`：因为是 `extra="ignore"`，老 host 发来的十五个字段照常解析，多出来的被丢掉。

这一项的 N/N−1 是免费的：控制面先改模型，host 后停发。

另外，`RetainedSandboxBeat.generation` 改名为 `sandbox_generation`，旧名用 `validation_alias` 接受一个版本（结构重构 §4 的术语规则）。

### 8.2 claim

`WorkerThreadSandbox`（`schemas/workers.py:171`）把调用方成员的字段又抄了一遍，同时 `group_members` 里也列了这个成员。改成：v≥k 的 claim 只带 group 级字段和 `group_members`，host 按 `thread_id` 找到自己。对老 host 继续发重复字段。host 端由 `control_plane_compat.py` 从 `group_members` 补出这些字段，之后只有一条代码路径。

### 8.3 存储

没有。

### 8.4 上线顺序

1. 控制面接受精简后的成员模型（`extra="ignore"`）。
2. host 停发多余字段。
3. claim 那一半：host 先能从 `group_members` 读，控制面再停发重复字段。

### 8.5 测试

- 用今天 host 发出的完整心跳做夹具，按旧模型和新模型分别解析，断言数据库写入完全相同。
- 迟到报告：在新进程已经写入之后，回放一份旧代数的报告，断言什么都不改。这条测试现在应该已经有了，要确认，没有就补上。
- A 层两个方向都要跑。

### 8.6 不在范围内

不改 reconcile 的语义；不删任何代数；不改 checkpoint 里的成员清单（`storage/repositories/sandbox/checkpoints.py:121`），那是另一份数据。

## 9. 10.4 控制面到 host 的命令队列

### 9.1 先分类：哪些通知需要队列

| 通知 | 现在怎么送 | 失败了靠什么兜底 | 需要队列吗 |
|---|---|---|---|
| 停止 group | 心跳响应 `retained_sandboxes_stop`，每次心跳按数据库状态重新计算（`api/hosts.py:514` 的 `due_thread_sandbox_stop`） | 下一次心跳会再发；host 按 group 和 activation 的代数判断 | 否：已经是电平触发、幂等的 |
| residency 丢失 | 三个直接推送（`application/residency.py:70`、`workers/attempts.py:154`、`:205`），加上心跳响应 `residencies_lost` 的回显 | 回显就是兜底：它按 host 报告的代数算出，带着代数回显 | 不为正确性；只为把三个推送点合并 |
| 取消 | attempt 心跳的 `cancel_requested` | 每次 attempt 心跳都会再带 | 否 |
| profiling 策略 | 单独轮询（`api/host_profiling.py:59`） | 下一次轮询 | 否，可以并进心跳 |
| SSH 访问变化 | `push_ssh_access` 尽力推送（`application/ssh_servers.py:441`），失败只记日志 | **没有**：注释写着"A host that cannot be reached gets the state with its next turn"。一次撤销如果没送到，正在跑的成员会一直持有密钥，直到下一轮 | **是** |
| 终端挂起和恢复 | host 先调控制面，控制面再回调 host（`api/worker.py:1193` 起） | 调用方的重试 | 待定 |
| send-now | 自己的 60 秒数据库租约（`application/send_now.py:42`） | 自己的租约 | 不在第一版 |
| 删除、archive | `workers/sandbox_deletion.py` 按自己的持久化状态推进 | 自己的状态机 | 否 |
| 交互式调用：文件、终端 I/O、git、状态、问答、Fork | 请求-响应 | 用户在等结果 | 否：同步应答本来就是目的 |

结论：队列是为"做不成也没有别的路径兜底"的意图准备的。第一版只装两种 `kind`：`ssh_access`（修一个真实的缺口）和 `residency_lost`（替换三个推送点，回显保留为兜底）。这改变了结构重构 §10.4 的迁移顺序，理由就是上表。

### 9.2 存储

新迁移，新表：

```sql
CREATE TABLE agent_host_commands (
    id            bigserial PRIMARY KEY,
    host          text        NOT NULL REFERENCES agent_runner_hosts(host),
    seq           bigint      NOT NULL,          -- 每个 host 单调递增
    kind          text        NOT NULL,          -- 'ssh_access' | 'residency_lost'
    target_kind   text        NOT NULL,          -- 'group' | 'member'
    target_id     text        NOT NULL,          -- sandbox_id 或 thread_id
    fence         jsonb       NOT NULL,          -- MemberFence 或 group fence
    payload       jsonb       NOT NULL,          -- 不含任何凭据值
    created_at    timestamptz NOT NULL DEFAULT NOW(),
    delivered_at  timestamptz,
    deliveries    integer     NOT NULL DEFAULT 0,
    next_delivery_at timestamptz NOT NULL DEFAULT NOW(),
    outcome       text CHECK (outcome IN ('applied','stale','failed_retryable','failed_terminal','expired')),
    outcome_detail text,
    settled_at    timestamptz,
    UNIQUE (host, seq)
);
CREATE INDEX agent_host_commands_open ON agent_host_commands (host, seq) WHERE settled_at IS NULL;
```

- `seq` 由 `agent_runner_hosts` 上的一个新计数列在同一事务里分配。
- **payload 不带凭据值。** `ssh_access` 只带 `revision`：host 收到后，用现有的拉取路径取完整状态，或者由下一次心跳带上同一个 revision 的完整状态（§12 Q6）。密钥不写进这张表。
- 结算了的命令保留 7 天，由现有的清理任务删除。

### 9.3 线上改动

- **下发。** 心跳响应加 `commands: [{seq, kind, target_kind, target_id, fence, payload}]`。取未结算、`next_delivery_at ≤ now` 的，按 `seq` 排序，最多 64 条。只发给协商到这个版本的 host。
- **加速。** `/worker/wait` 的响应加 `commands_pending: bool`。为真时，host 立刻发一次心跳，不等 30 秒的间隔（`host_pool.py:69`）。直接 HTTP 推送也保留，但只用来"踢一下"，失败不重试，正确性只依赖队列。
- **ack，按条，不累计。** 下一次心跳请求带 `command_results: [{seq, outcome, detail}]`。
  - `applied`、`stale` 算完成，写 `settled_at`。
  - `failed_retryable` 按指数退避设置 `next_delivery_at`，上限 5 分钟。
  - `failed_terminal` 写 `settled_at`。
  - 控制面在一条命令 `deliveries > 20` 或者存在超过 1 小时后，把它结算为 `expired`。
- **host 端去重。** host 把最近执行过的 `(seq, outcome)` 存在本地状态目录里，保留 1,000 条。重复收到同一个 `seq`，直接回报存下的结果，不再执行。这只防"同一条记两遍"；真正的安全来自下面的效果幂等。

### 9.4 效果幂等和顺序

每种 `kind` 都写明重放时的行为，并按 `(kind, target, fence)` 判断是否已经做过：

| kind | fence | 效果 | 重放 | 判为 stale | 判为 failed_retryable |
|---|---|---|---|---|---|
| `ssh_access` | group 的 `sandbox_id`、`sandbox_generation`；payload 的 `revision` | 把成员 tmpfs 里的 SSH 状态换成这个 revision，并切断已关闭服务器的会话（现有的 `sync_ssh_access`） | 已应用的 revision ≥ payload 的，什么都不做，回报 `applied` | group 的代数不是 fence 里的，或者 group 已经不在这台 host 上 | Sandbox Manager 不可达；容器正在启动或停止（`SessionBusy`） |
| `residency_lost` | 成员的 `sandbox_id`、`sandbox_generation`、`residency_generation` | 隔离这个 residency 的成员进程（现有的 `shared_residency_lost`） | 成员当前的 residency 代数 ≠ fence，或者进程已经隔离，回报 `applied` | group 代数变了；成员已经不在 | Sandbox Manager 不可达 |

**同一个目标按 `seq` 依次执行。** host 对同一个 `target_id` 的命令串行，不同目标之间并行。较新的 fence 让较旧的命令变成 `stale`，不能反过来：host 本地代数大于 fence 时回报 `stale`；小于 fence 时（host 还没赶上），回报 `failed_retryable`，不执行。

**做不成的命令。**

- `failed_terminal` 和 `expired` 记为 `agent_host_command_failed` 指标，在管理界面的 host 详情里列出。
- `ssh_access` 做不成时，兜底是对这个 group 下一次停止：控制面把它标记为 `stop_requested`，由现有的电平触发停止路径执行。停了就没有正在跑的成员持有被撤销的密钥。
- `residency_lost` 做不成时，`residencies_lost` 的回显照旧兜底，而且 relay 已经在每次请求时按数据库拒绝这个占位凭据（AGENTS.md §7）。

### 9.5 N/N−1

- 老 host 不认识 `commands`，会忽略它；控制面不给协商版本不够的 host 入队，对它们继续直接推送。
- 老控制面不发 `commands`，新 host 照旧接受直接推送。
- 三个 `shared_residency_lost` 推送点和 `push_ssh_access` 在一个兼容期内保留，只对老 host 生效，标 `COMPAT`。

### 9.6 上线顺序

1. host：能执行命令、回报结果、做本地去重；并在心跳里报告协商版本（§4）。
2. 控制面：建表；对新 host 入队并下发；对老 host 继续推送。
3. 先迁 `ssh_access`，观察一周的 `failed_*` 和 `expired` 计数，再迁 `residency_lost`。
4. 下一个发布删掉推送的兼容分支。

### 9.7 测试

- 状态机的表驱动：每个 outcome × 次数 → `next_delivery_at`、`settled_at`。
- 幂等：同一条命令执行两次，效果和执行一次相同；host 在执行之后、ack 之前崩溃，重新下发后回报 `applied`。
- 顺序：同一目标上 fence 递增的三条命令乱序到达，结果和顺序到达相同；较旧的回报 `stale`。
- 不可达：Sandbox Manager 停掉一段时间，命令变成 `failed_retryable`；恢复后被执行；超时后变成 `expired`，并触发 `ssh_access` 的停止兜底。
- 写入量：在 staging 统计一周每台 host 每天的命令数，写进实现 PR（结构重构 §15 的"命令队列的成本"）。按现在的推送频率估计，每台 host 每天是几十条的量级。
- B 层：序列 B 的第 4 步，撤销一个 SSH 服务器时，Sandbox Manager 先断开 60 秒，断言恢复后撤销被执行。

### 9.8 不在范围内

send-now；终端挂起和恢复；环境运行；删除和 archive；任何交互式调用；把 attempt 心跳并进 host 心跳（结构重构 §12）。

## 10. 总的排期

| 顺序 | 项 | 改协议吗 | 依赖 |
|---|---|---|---|
| 1 | §3 A 层（已实现，见 §3.5） | 否 | — |
| 2 | §6.2 residency 值进入脱敏器 | 否 | — |
| 3 | §3 B 层 | 否 | 1 |
| 4 | §4 协议版本（v2） | 是 | 1、3 |
| 5 | §7 能力匹配 | 只改控制面 | — |
| 6 | §8 成员上报 | 是 | 4 |
| 7 | §5 `MemberFence`（v3），residency 收紧另升一版 | 是 | 4 |
| 8 | §9 命令队列 | 是，加一张表 | 4、7 |
| 9 | §6.3 线上封装 | 是，可能不做 | 2、4 |

第 2 项和第 5 项不依赖混合版本测试，可以先做。其余协议改动的每个 PR，两层测试都要在那个 PR 上跑绿。

## 11. 不做的事

- 不合并 sandbox 路由；不合并 relay 的 listener 和隧道；不把 attempt 心跳并进 host 心跳（结构重构 §12）。
- 不改凭据的投递时机和格式。
- 不让混合版本测试代替 staging 验收：它不跑 Kata，不跑真实的 SSH 隧道。
- 不为混合版本测试给 sandbox 加放宽出口的开关（AGENTS.md §7）。

## 12. 待决问题

- **Q1：B 层的模型 provider。** 用真实 provider 的 CI 密钥（覆盖完整），还是只验证"失败的一轮"（不需要密钥）？密钥由谁的账户出、额度多少、轮换谁负责？
- **Q2：上一版 control 镜像的架构。** 在 x64 上用上一版源码构建 control 镜像，测的就不是发布出来的那份字节。要不要在发布时也给 control 打 amd64，或者把 B 层拆到 arm64 和 x64 两台机器上？前者增加发布时间，后者要跨机器组网。
- **Q3：residency 加 sandbox 和 activation fence。** 建 residency 时，activation 是否一定已经确定？如果有 Fork 或者重建 residency 的路径不满足，这一步就不能做。
- **Q4：滚动升级检查也该用上一版自己的 venv。** 现在它用本版的依赖跑上一版的代码。要不要顺手改？
- **Q5：固定 host 缺能力的轮次。** 能力匹配挪进 SQL 以后，这样的轮次会一直等待。要不要在等待超过某个时间后失败，并告诉用户原因？
- **Q6：`ssh_access` 命令怎么拿到完整状态。** 命令里不放密钥。host 是按 revision 主动拉取（要加一个 host → 控制面的路由），还是由心跳带上完整状态（心跳里就会出现密钥）？倾向前者。
- **Q7：终端挂起和恢复要不要进队列。** 它现在是一次往返的回调。如果 host 在回调之前崩溃，终端是否会一直挂起？要先查清楚现有的重试，再决定。
