# GitHub 集成加载失败后自动恢复

日期：2026-09-24。状态：已实现，随修复 PR 一同提交；实现与本文的差异见第 8 节。

关联 issue：[GitHub #47 — Source control loads once at boot](https://github.com/HarvardMadSys/hybridInference-cloud-agent/issues/47)。

代码核对基线：远端 `dev`，提交 `10482fd1071d22f04c185009742b492200a0fd24`。实施前同步最新 `dev` 并读取 AGENTS.md。

修订记录：

- 2026-09-22：初版；同日去掉管理员诊断接口与 Servers 页面板。
- 2026-09-23：按评审意见精简。是否启用 GitHub 只看 App ID；不新增后台重试任务，改为取凭据时按 30 秒间隔重试；GitLab 不重试；用户只看到"暂时不可用"，失败原因只写日志；删除为管理员面板准备的状态字段。
- 2026-09-24：实现完成，基线同步到 `dev` 的 `42733427`；第 8 节改为实现记录。

## 1. 问题

控制平面只在启动时读取一次 GitHub App 私钥。读取失败后，凭据在整个进程生命周期内都是 `None`，日志里只有一条 WARNING。

2026-08-07 生产上线时，容器启动那一刻私钥文件还不属于容器用户，读取报 `PermissionError`。6 分钟后权限修好了，但服务不会再读，只能重启才能恢复。

故障期间，Integrations 页面显示 "Admin setup required"，任务入口提示联系管理员，看起来像是根本没配 GitHub。管理员检查配置发现全都正确，无从下手。

另外，发 PR 的 publisher 循环和处理 goal 的 reaper 循环，在启动时就把凭据传了进去，之后不再读取。所以只更新 `app.state` 不够，它们仍会拿着旧的 `None`。

## 2. 目标与范围

必须做到：

1. 私钥文件的权限、挂载或内容修好后，同一进程在一分钟内自动恢复，不需要重启。
2. HTTP 请求、publisher、goal 处理都使用恢复后的凭据。
3. 故障期间，用户只看到"GitHub 暂时不可用"；不误报"没有权限"或"需要管理员配置"，也不把私有仓库当作匿名仓库交给 runner。
4. 故障期间不丢工作、不消耗重试次数：排队的任务、待发布的 PR、goal 的下一轮都保留，恢复后继续；正在运行的任务照常结束。
5. 失败原因写入 ERROR 日志，恢复时记一条 INFO。
6. 没配 GitHub 的部署行为不变，包括 allowlist 和公开仓库路径。

不做：

- 在页面上展示失败原因；
- 管理员诊断接口或面板；
- 修改环境变量后自动生效（仍需重建进程）；
- 加载成功后热更换私钥；
- GitLab 重试；
- MCP 配置问题（issue 里 `/dev/null` 挂载空 MCP 配置只是背景）。

## 3. 已确认的代码事实

后端路径省略前缀 `apps/backend/cloud_agent/`，行号以基线提交为准。

| 位置 | 现状 |
| --- | --- |
| `bootstrap/lifespan.py:50` `_build_source_control` | 启动时构建 GitHub/GitLab 客户端；出错只记 WARNING，留下 `None`。主 API 和 MCP relay 共用这个 lifespan |
| `bootstrap/lifespan.py:102`、`:196` | publisher 和 reaper 在启动时捕获 `app.state.app_credentials`，之后不再读取 |
| `api/deps.py:130` `get_agent_app_credentials` | 直接返回 `app.state.app_credentials`。25 个路由依赖它，测试通过 `dependency_overrides` 注入 fake |
| `github_app.py:86` `AppConfig.from_env` | App ID 或私钥为空时返回 `None`；读取私钥文件失败时抛 `GitHubAppError`，其 `__cause__` 是 `OSError`。不校验 PEM，无效私钥要等第一次签 JWT（`github_app.py:114` `build_app_jwt`）才报错 |
| `source_control.py:164`、`:56` | GitLab 配置和加密密钥全部来自环境变量。全空时返回 `None`；部分配置、redirect URI 不合法或缺少加密密钥时抛 `SourceControlError` |
| `deploy/control-plane/docker-compose.yml:159`、`:208` | 私钥路径总有默认值 `/etc/cloud-agent/github-app.pem`；没配 App 时挂载 `/dev/null`，读到的是空文件 |
| `api/worker.py:544` `worker_claim` | 凭据为 `None` 时只按 allowlist 判断权限，通过后不带 token 交给 runner。GitHub 临时故障已有"退回队列并返回 503 `credential_unavailable`"的先例 |
| `api/worker.py:1590` `worker_finish` | 凭据只用于在确认完成前调用 `continue_after_finish` 处理 goal wakeup，该调用有单独的 try/except |
| `goals/controller.py:91` `run_goal_wakeups` | 先 `claim_wakeups`（`storage/repositories/goal_wakeups.py:141` 会让 `attempts + 1`），出错走 `_retry_or_pause`，满 5 次暂停 goal。凭据为 `None` 时，`_check_access` 会把不在 allowlist 里的仓库判成 `access_denied` 并暂停 goal（`:459`） |
| `publish_worker.py:427` `publish_loop` | 每 15 秒一轮；两种凭据都没有时跳过本轮 |
| `workers/attempts.py:593` | reaper 每 30 秒调用一次 `run_goal_wakeups`，放在单独的 try/except 里，不影响其他清理工作 |
| `storage/repositories/goals.py:636` | goal 停滞检查会跳过有 pending wakeup 的 goal |
| `apps/backend/cloud_agent_host/runner.py:3449` | runner 领取任务出错时记日志，等待后重试 |
| `operational_health.py` `queue_alarm` | 有排队任务但没有任务在运行时，发出 `queue_not_starting` 告警 |
| 前端 `IntegrationsView.tsx:101`、`:120` | 未配置时显示 "Admin setup required"；已有 "Temporarily unavailable" 文案 |
| 前端 `lib/i18n/dictionaries/en.ts:415` | 任务入口在没有 install URL 时提示 "Contact your administrator…" |
| 前端 `useAgentJobs.ts:525` | goal 处于 active 时页面持续轮询 |

## 4. 设计

### 4.1 三种状态

| 状态 | 条件 | `get()` 的结果 |
| --- | --- | --- |
| `unconfigured` | `AGENT_GITHUB_APP_ID` 去掉空白后为空 | 返回 `None` |
| `unavailable` | App ID 有值，但私钥缺失、为空、读取失败或无效 | 抛 `SourceControlUnavailable` |
| `ready` | 私钥读取成功，并且能在本地签出 App JWT | 返回凭据对象 |

**是否启用 GitHub 只看 App ID。** compose 部署里私钥路径总有默认值，不能用它判断，否则没配 App 的部署会被误判为 `unavailable`。App ID 为空时不读取私钥，结果与现在相同，都是 `None`。

`ready` 只说明本地加载成功。GitHub API 超时、App 未安装、用户撤销授权等情况仍走现有错误处理，不改变加载状态。

### 4.2 加载与重试

新增一个小对象，例如 `cloud_agent/source_control_runtime.py` 中的 `GitHubAppProvider`。它由 lifespan 创建，挂在 `app.state.source_control` 上，取代 `app.state.app_credentials`。

- 启动时读取一次环境变量，并立即尝试加载一次。
- `get()`：
  - `ready`：返回缓存的对象；
  - `unconfigured`：返回 `None`；
  - `unavailable`：如果距上次尝试已满 30 秒，先同步重试一次，成功就直接返回；否则抛 `SourceControlUnavailable`。
- 不新增后台重试任务。publisher（每 15 秒）、reaper（每 30 秒）和 HTTP 请求都会调用 `get()`，所以故障修好后一分钟内就能恢复。
- 每次重试都真正重新打开私钥文件，不能只看 mtime，因为 `chown` 只会改变 ctime。读取后用现有的 `build_app_jwt` 在本地签一次作为校验，不访问 GitHub。
- 请求线程池和事件循环里的后台循环可能同时调用 `get()`。重试部分用一把 `threading.Lock`；拿不到锁的调用不等待，按当前状态返回。
- 加载成功后一直复用同一个对象，保留 token 缓存，不再重新加载。
- 关闭服务时，由 lifespan 调用 provider 的 `aclose()`，关闭已加载对象的 HTTP client，取代 `lifespan.py:300` 现有的关闭逻辑。
- 30 秒间隔和时钟都做成可注入的参数，测试时不真实等待。
- MCP relay 也走这个 lifespan，但它没有 GitHub 环境变量，状态会是 `unconfigured`，不需要特殊处理。

### 4.3 GitLab

GitLab 的配置全部来自环境变量，进程运行期间不会变，重试不可能成功，所以不做重试。现有的 `from_env` 已经能区分"全空"和"配置错误"，这次只需：

- 配置错误从 WARNING 改为 ERROR；
- 在 Integrations 响应中报告 `load_status`；
- `get_agent_gitlab_oauth` 保持现有行为，失败时仍视为 `None`；
- 页面继续显示 "Coming soon"。

### 4.4 日志

- 第一次失败，或失败原因发生变化时，记 ERROR，事件名 `source_control_load_failed`。字段包括 provider 和原因类别：`key_file_unreadable`、`key_missing`、`key_invalid`、`unexpected`。私钥文件问题要附上文件路径和系统错误（例如 Permission denied），并附堆栈。
- 同一原因重复失败时不再记 ERROR，避免每 30 秒刷一次屏。
- 恢复时记 INFO，事件名 `source_control_load_recovered`，包含 provider 和故障持续时间。
- 未配置属于正常情况，不记 ERROR。
- 日志中不得出现私钥内容、token 或 client secret。原因类别根据异常类型和 `__cause__` 判断，不匹配异常文本。
- 故障持续时，排队任务无法被领取，现有的 `queue_not_starting` 告警会触发，不需要新增告警。

## 5. 接线

### 5.1 HTTP 依赖

- `get_agent_app_credentials` 改为严格模式：`unavailable` 时返回 503，其他情况行为不变。默认严格，以后新增路由即使忘了处理，也不会把故障当成"未配置"。
- 新增宽松依赖 `get_github_app_state`，一次读取同时返回凭据和加载状态；`unavailable` 时凭据为 `None`，不抛错。两者不能分两次读：中间若恰好有一次重载完成，会得到"没有凭据 + ready"，页面会把它显示成"未配置"。只用于以下入口：
  - `list_source_control_integrations`（`GET /integrations`）；
  - `get_agent_config`（`GET /config`）；
  - 三个断开连接的路由：`disconnect_github_integration`、`disconnect_gitlab_integration`、旧版 `disconnect_github`。照常删除本地连接，上游撤销按现有方式尽力而为。
- `worker_finish` 不再依赖凭据，改为把 provider 传给 `continue_after_finish`（见 5.4）。
- 其余依赖凭据的路由都保持严格，包括建任务、追问、on-call 建任务、连接 GitHub、分支列表、Skills 导入、文件与终端入口、`worker_claim`。
- 503 响应如下，并带 `Retry-After: 30`：

```json
{
  "detail": {
    "error": {
      "type": "source_control_unavailable",
      "provider": "github",
      "message": "GitHub is temporarily unavailable. Please try again shortly."
    }
  }
}
```

- 503 在依赖里直接抛出，不要在路由内部用宽泛的 `except` 吞掉。
- 如果没有挂 provider（测试没走 lifespan），两个依赖都按 `unconfigured` 处理，与现在的 `getattr(..., None)` 一致。T13 负责确认真实 lifespan 会挂上 provider。
- 原来覆盖 `get_agent_app_credentials` 的测试，如果针对的入口改用了宽松依赖或 provider，要改为覆盖新的依赖，不得删改断言。

### 5.2 worker claim 与 finish

- `worker_claim` 使用严格依赖，故障时在 `claim_job` 之前就返回 503：不占用重试次数、不签发 grant，也不会让 runner 匿名 checkout。runner 现有逻辑会等待后重试，任务一直留在队列里。
- `worker_finish` 照常校验 fence、保存结果、登记 wakeup，并确认成功。goal 的处理交给 5.4 的拦截：故障期间 wakeup 保持 pending，恢复后由 reaper 处理。goal 处于 active 时页面会继续轮询，所以恢复后会自然显示下一轮。

### 5.3 publisher

- `_start_publisher` 传入 provider，不再传启动时的值。
- 每轮开始时调用一次 `get()`。如果是 `unavailable`，就跳过本轮：保留待发布的快照和重试记录，不改用 `AGENT_GITHUB_TOKEN`，也不标记发布失败。
- 本轮取到的对象传给 `retry_snapshot_base_import` 和 `publish_one`。
- 恢复后由原来的循环继续发布，不启动第二个 publisher，也不重复创建 PR。

### 5.4 goal

- `reap_expired_attempts`、`run_goal_wakeups` 和 `continue_after_finish` 改为接收 provider（或一个取凭据的函数），不再接收凭据对象。
- `run_goal_wakeups` 在 `claim_wakeups` 之前调用 `get()`。如果是 `unavailable`，直接返回 0：不领取 wakeup、不增加 attempts、不进入 `_check_access`、不走 `_retry_or_pause`。
- reaper 和 `worker_finish` 都走这一个入口。
- reaper 的其他工作照常执行，包括租约回收、grant 续期与撤销、清理和 goal 调度检查。故障期间 wakeup 一直是 pending，停滞检查不会误暂停 goal；用 T09 锁住这一点。

## 6. 用户看到的行为

### 6.1 API

- `SourceControlProviderResponse` 增加 `load_status: "unconfigured" | "unavailable" | "ready"`，GitHub 和 GitLab 都要填。
- `AgentConfigResponse` 增加 `github_load_status`，取值同上。
- 只报告状态，不带失败原因。
- GitHub 为 `unavailable` 时：
  - Integrations 中 `configured=false`，`connect_url`、`manage_url` 为 null，`repositories` 为空；
  - `connected` 和 `accounts` 照常从用户自己的连接记录读取，不清空；
  - config 中 `github_connected=false`、`github_install_url=null`。
- 所有构造 `SourceControlProviderResponse` 的地方都要填这个字段，包括 connect 和 disconnect 返回的整页响应。

### 6.2 前端

- `lib/api/agents.ts`：新字段设为可选。字段缺失时（旧后端）沿用现有行为。
- Integrations 页面：
  - GitHub `unavailable` 且未连接：显示现有的 "Temporarily unavailable"，不显示 "Admin setup required"；
  - GitHub `unavailable` 且已连接：保留账户名，用 "Temporarily unavailable" 替换 "Connected" 和仓库数量；断开连接仍可用；
  - GitLab 仍显示 "Coming soon"。
- 任务入口（`ConnectSourceControl.tsx`、`TaskComposer.tsx`）：`github_load_status === "unavailable"` 时显示新的"暂时不可用"文案，例如 "GitHub is temporarily unavailable" 和 "Repository access will come back automatically. Try again in a minute."，同时补上 `zh-CN`；不显示"联系管理员"。
- 页面处于"暂时不可用"时，每 30 秒重新获取一次状态；恢复后自动回到正常显示，并停止重新获取。
- 只采用最后一次发出的请求的结果：在断开连接之前发出、之后才返回的复查响应，不能把已删除的连接显示回来。
- 创建任务等操作收到 503 `source_control_unavailable` 时，显示响应里的 message。

## 7. 测试

使用注入的时钟和加载函数，不真实等待 30 秒。至少一个测试要运行真实的 provider 和真实的 publisher 或 reaper 循环，不能只手动修改状态。

| 编号 | 场景 | 必须断言 |
| --- | --- | --- |
| T01 | App ID 为空，私钥路径为 compose 默认值，文件为空 | 状态为 `unconfigured`；没有 ERROR；allowlist 和公开仓库行为不变 |
| T02 | App ID 有值，读私钥抛 `PermissionError`，之后变为可读 | 先是 `unavailable`，记一次 ERROR；30 秒后同一进程变为 `ready`，记一次 INFO |
| T03 | 私钥为空或 PEM 无效，之后原地修好 | 状态为 `unavailable`，原因类别正确；修好后变为 `ready`；内联私钥优先于文件、转义换行的处理不变 |
| T04 | 30 秒内多次调用；并发调用 | 30 秒内不重复加载；并发时只加载一次 |
| T05 | 故障期间的 HTTP 入口 | 建任务、追问、文件入口返回 503 和 `Retry-After`；Integrations 与 config 返回 200 和 `load_status="unavailable"`，保留已连接账户；断开连接成功；恢复后一切正常 |
| T06 | 故障期间的 `worker_claim` | 返回 503；没有调用 `claim_job`、没有签发 grant；任务仍在队列，attempts 不变；恢复后可以正常领取 |
| T07 | 故障期间的 `worker_finish` | 结果已保存且确认成功；wakeup 仍是 pending；attempts 不变 |
| T08 | publisher 跨越故障与恢复 | 故障期间不领取发布任务、不标记失败、不改用 static token；恢复后原循环发布一次，不重复创建 PR |
| T09 | goal 跨越故障与恢复（超过 5 个 reaper 周期） | 故障期间不领取 wakeup，attempts 不变，goal 保持 active，没有 `access_denied` 或 `scheduling_failed`；reaper 其他工作照常；恢复后原 wakeup 被正常处理 |
| T10 | 日志 | 同一原因只记一次 ERROR；原因变化时再记；恢复时记一次 INFO；日志里没有私钥内容 |
| T11 | GitLab 部分配置 | 状态为 `unavailable`，记 ERROR；不重试；不影响 GitHub |
| T12 | 前端 | `unavailable` 时显示 "Temporarily unavailable"，未配置时仍显示原文案；已连接账户保留；任务入口不提示联系管理员；恢复后自动恢复正常；旧后端缺字段时行为不变 |
| T13 | lifespan 接线 | 真实 `create_app()` 的 lifespan 创建了 provider，并把 provider（而不是凭据对象）传给 publisher 和 reaper；关闭时关闭已加载的 client |

执行检查：

```bash
# 仓库根目录
make check

# apps/frontend，按 lockfile 安装依赖后
npm run type-check
npm run lint
npm run format:check
npm test
```

T06、T07、T09 涉及队列和 wakeup 的持久化，需要用 dbtest 基础设施验证，并运行 `make test-db`（`make check` 不包含 dbtest）。缺少 Postgres 时如实报告 NOT_RUN，不能用 mock 代替持久化验证。

真实文件权限的测试需要在非 root 用户下运行，或者在读文件的地方注入 `PermissionError`。root 用户执行 `chmod 000` 后仍然能读取，这种情况不算通过。

**关闭 #47 的条件**：以上行为实现并验证，修复合并到 `dev`。PR 中单独说明是否已部署；没有部署证据时，不能宣称线上已经恢复。

## 8. 实现记录

实现基于 `dev` 的 `42733427`，与上文设计一致，以下是命名和几处需要说明的细节。

| 位置 | 实现 |
| --- | --- |
| `source_control_runtime.py` | `GitHubAppProvider`（`get()`、`state()`、`load_status`、`aclose()`）、`GitHubAppState`、`GitLabOAuthLoad`、`SourceControlRuntime.from_env()`、`SourceControlUnavailable`、`GitHubAppSource` 协议 |
| `api/deps.py` | `get_source_control`、严格的 `get_agent_app_credentials`、宽松的 `get_github_app_state`（返回 `GitHubAppState`：凭据与状态来自同一次读取）、`get_gitlab_load_status`、503 构造 `source_control_unavailable` |
| 后台循环 | `publish_loop`、`reap_expired_attempts`、`run_goal_wakeups`、`continue_after_finish` 的参数由 `app_credentials` 改为 `github_app` |
| 前端 | `sourceControlRecovery.ts` 定义 30 秒复查间隔；Integrations 与任务入口显示"暂时不可用" |

与设计的差异：

- `GitHubAppProvider` 多了一个 `credentials_factory` 参数，默认就是 `GitHubAppCredentials`。测试用它在真实读取并签名私钥之后换成假的 minter，使恢复后的 HTTP 测试不访问 GitHub。
- `worker_finish` 不再依赖凭据，而是取 `get_source_control` 并把 provider 交给 `continue_after_finish`，比"宽松依赖"更直接。
- "只有 5 个路由使用宽松依赖"由 `test_only_the_routes_that_describe_the_state_tolerate_an_unloaded_app` 锁定，新路由默认严格。
- 日志沿用 `cloud_agent.app` logger，与原来那条 WARNING 的位置一致。
- T06、T07 的持久化：`worker_claim` 的 503 在依赖阶段抛出，处理函数和数据库都不会执行；`worker_finish` 的终态写入代码没有改动。因此数据库层只新增了 T09 的真实 Postgres 测试，T06、T07 用路由测试验证。

评审后的修正（2026-09-24）：

- 宽松路由原先分两个依赖读取凭据和状态，重载恰好在两次读取之间完成时，会得到"没有凭据 + ready"。现在由 `GitHubAppProvider.state()` 一次读取同时给出两者。
- Integrations 页面的定时复查与断开连接后的刷新存在先发后到的竞态，旧响应会把已删除的连接显示回来。现在只采用最后一次发出的请求的结果。
- 测试里的假私钥不再使用 PEM 头尾格式，避免 Gitleaks 的 `private-key` 规则误报。
- 评审还提出恢复 `load_error`（诊断码、原因和处理建议）。这来自 2026-09-22 的旧版文档；2026-09-23 已决定页面只显示"暂时不可用"，原因只写日志，因此不恢复。

测试对应：

| 编号 | 测试 |
| --- | --- |
| T01–T04、T10、T11，以及读取一致性 | `tests/unit/test_source_control_runtime.py`（含 `test_a_reading_taken_during_a_reload_never_pairs_no_credentials_with_ready`） |
| T05–T07 | `tests/servers/test_agent_jobs_api.py` 中的 `*unloaded*` 测试 |
| T08 | `tests/unit/test_agent_publish_worker.py::test_the_running_loop_waits_out_an_unloaded_app_and_then_publishes_once` |
| T09 | `tests/unit/test_goal_controller.py::test_an_unloaded_github_app_leaves_the_wakeup_unclaimed_until_it_loads`，以及 dbtest `tests/integration/storage/test_goal_store.py::test_an_unloaded_github_app_leaves_the_goal_waiting_on_its_rows` |
| T12 | `IntegrationsView.test.tsx`、`TaskComposer.test.tsx` 中的相关用例，含旧复查响应不得覆盖断开连接的回归测试 |
| T13 | `tests/unit/test_source_control_runtime.py::test_the_lifespan_hands_the_loops_the_provider_not_a_snapshot` |

修复合并后，还需要部署到环境才能确认线上恢复；在此之前 #47 只能算代码已修复。
