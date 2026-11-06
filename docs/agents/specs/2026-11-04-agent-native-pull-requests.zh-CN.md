# Agent 自己开 PR：GitHub 中继与用户署名

日期：2026-11-04。状态：已决定，在一个 PR（#1103）里实现（见第 9 节）。

设计核对基线：远端 `dev`，提交 `1e064e169`。

相关：[按需发布](2026-10-15-manual-publication-design.zh-CN.md)（本文取代它）、[公网出口](2026-09-23-agent-public-egress-design.zh-CN.md)、[sandbox 安全模型](2026-09-01-agent-sandbox-security-model.zh.md)、[去掉 PR 合并后自动归档](2026-10-19-remove-pr-merge-archive.zh-CN.md)。

## 1. 结论

云端 agent 像本地 Claude Code 一样工作：用户在对话里说"开个 PR"，agent 自己 commit、按仓库规则跑检查、`git push`、开 PR。标题、正文、commit message 都由 agent 写。

- **PR 和 commit 属于用户本人。** commit 作者是用户（GitHub noreply 邮箱），agent 写成 `Co-Authored-By: FreeInference`；push 和 API 调用以用户本人的 GitHub 身份发出。
- **真正的 GitHub 凭据不进 sandbox。** sandbox 只拿到一个对话级的占位凭据。git 和 `gh` 的 GitHub 流量经过平台的 GitHub 中继，中继校验占位凭据、检查仓库和操作，再换成用户的 GitHub App user token 转发给 GitHub。这和 Claude Code cloud 的 GitHub proxy 是同一种做法，也和我们自己的 MCP relay 是同一种结构。
- **平台不再替 agent 开 PR。** 删掉快照捕获、补丁闸门、`agent/<thread-id>` 发布分支、Create PR / Update PR 按钮，以及 sandbox 里现在的 `GH_TOKEN`。

## 2. 为什么改

PR #1094 是平台按钮开出来的，暴露了这套方式的问题：

- **该给审阅者看的东西没进 PR。** agent 的收尾消息写了根因、改动、跑了哪些检查、哪些没跑成；PR 正文却是平台模板：快照编号、workspace epoch、`ajob_` 编号，加用户的原话和文件列表。标题取收尾消息第一行，成了 `[agent] 修好了。`（`pr_titles.py` 只认英文的空话）。commit message 是 `agent: <用户原话>`，作者 `agent@localhost`。
- **agent 不知道平台怎么发布。** 平台不给 agent 任何说明，它按仓库 CLAUDE.md 的习惯反问"要我提交并开 PR 吗"，但它开不了，要用户自己去点按钮。
- **两条路互相矛盾。** 2026-10-11（`04381888d`）起 sandbox 已经拿到一个仓库级 installation token（`GH_TOKEN`），可以直接 push 和开 PR，而且不过补丁闸门；但 Watch PR、code review 的提示词和 PR 模板仍然写着"sandbox 没有 GitHub 凭据"。
- **用户的心智模型是"说开就开"。** 本地 Claude Code 就是这样，平台没有理由不同。

## 3. 云端和本地有什么不同

对 agent 的信任没有不同：都是用户自己发起、替用户干活，都会读外部内容、都可能无人看管地跑。所以工作流完全一样。

不同的是**谁保管凭据**。本地的 token 在用户自己的 keyring 里；云端的平台替所有用户保管凭据，sandbox 跑在共享的宿主机上。

| 被攻破的是 | 现在（sandbox 持有 installation token） | 本设计（中继） |
|---|---|---|
| 单个 sandbox | 这个用户这个仓库一小时的写权限 | 一个占位凭据；离开这个对话的租约就没用 |
| 宿主机 | 机器上每个运行中对话各一个仓库的 token | 占位凭据；真正的 token 不在宿主机上 |
| 控制面 | 所有用户（user token 和 App 私钥本来就在这里） | 相同，没有变化 |

GitHub 的 user token 不能限定到单个仓库，所以绝不能放进 sandbox；中继让我们既用用户本人的身份，又把仓库范围的检查放在 sandbox 够不着的地方。

## 4. GitHub 中继

### 4.1 位置和路径

```
sandbox 里的 git / gh / curl
  │  https://github.com/...、https://api.github.com/...
  │  （/etc/hosts 把这两个名字指向组桥上的 github-tunnel）
  ▼
github-tunnel（宿主机，组桥上的 relay 别名，TCP 443）
  │  ssh -L，独立密钥，permitopen 只到控制面的 GitHub 中继端口
  ▼
github-relay（控制面，第三个 listener，只含 GitHub 中继路由）
  │  TLS 在这里终止；证书由部署自己的 CA 签发，名字限定为 github.com 和 api.github.com
  │  校验占位凭据 → 检查仓库和操作 → 换成用户 token
  ▼
github.com / api.github.com
```

- **一个只做这件事的 listener。** 和 `create_mcp_app` 同一个镜像、同一个数据库，`create_github_relay_app` 只包含 GitHub 中继路由。它不进公网边缘，也不和 MCP relay 共用端口：MCP relay 的 token 不能在这里用，反过来也一样。
- **独立隧道。** 宿主机上新增 `github-tunnel` 容器，用自己的 SSH 密钥和 `permitopen`，挂在 `agent-relays` 网络上，由现有的 relay 别名机制接到每个组桥上，端口 443 进入组桥的 `relays` 集合。理由和 MCP 隧道单独一条一样：一把能开两条隧道的钥匙会让隔离变成摆设。
- **名字解析。** `github-tunnel` 在 `agent-relays` 网络上带着 `github.com` 和 `api.github.com` 两个别名，由现有的 relay 机制接到每个组桥上：runc 下 Docker 自带的 DNS 直接用别名回答（已在本机验证），Kata guest 里由可信 helper 维护的 `/etc/hosts` 块写入同样的地址。其他 GitHub 域名（`codeload`、`raw.githubusercontent.com`、`objects.githubusercontent.com` 等）不变，仍然直连公网。
- **CA 信任。** 部署生成一个只允许签 `github.com`、`api.github.com` 的 CA（X.509 name constraints），私钥只在控制面。网桥把这两个名字解析到中继，对 sandbox 里的**每个**进程都是这样：有占位凭据的成员、临时工作区里没有凭据的成员、环境的 setup/services/verify 脚本、action、终端。所以信任这个 CA 不能跟"持有 GitHub 凭据"绑在一起，否则 setup 脚本 `git clone` 一个公开仓库就会 TLS 失败。容器里第一个进程启动之前，宿主机以 root 在 `/etc/freeinference/` 写入这个 CA 和一份合并后的信任库（镜像自带的系统根证书加上这个 CA），root 所有、`0444`，任何成员、脚本和终端都改不了；每个进程都带上 `SSL_CERT_FILE`、`GIT_SSL_CAINFO`、`CURL_CA_BUNDLE`、`REQUESTS_CA_BUNDLE`、`NODE_EXTRA_CA_CERTS`，使 git、gh、curl、Node 和 Python 都信任它，进程自己设置了同名变量时以它自己的为准。镜像的信任库不受影响，也不需要改容器的创建参数。

**hosts 覆盖不是安全边界，只是方便。** agent 可以绕开 hosts 直接用 GitHub 的真实 IP（出口本来就允许公网 443），但 sandbox 里没有任何真正的 GitHub 凭据，绕过去只能匿名访问 GitHub。安全边界是"真 token 只在中继里"，不是"sandbox 到不了 github.com"。

### 4.2 占位凭据

- 每个成员在建立 residency 时得到一个 GitHub 中继 token：和 MCP 的 `amr.` token 同一套 HMAC 格式（`tokens.py`），但 scope 是 `github-residency`，两者互不通用。
- 它通过 credential helper 交给 git，通过 `GH_TOKEN` 交给 `gh`。它只对中继有意义，直接发给 GitHub 会被当成无效 token 拒绝。
- 每次请求都按 MCP relay 的方式验证（`resolve_residency_attempt` 加 fence 复查）：residency 代数一致、状态 hot、租约未过期，对话没删，当前 attempt 在运行，宿主机还在册。用户身份从 job 读，不从请求里读。
- 撤销是隐式的：Stop、取消、attempt 被替换、residency 轮换、宿主机移除，token 都立即失效。

### 4.3 用户 token

- 中继用 `agent_github_accounts` 里这个用户的 GitHub App user token（8 小时有效，6 个月的 refresh token）。刷新复用 `github_user_token.user_access_token` 的租约和代数 fence。
- 因为由控制面刷新，长时间的对话不再遇到 installation token 一小时过期的问题。
- 用户没连接 GitHub 或 refresh token 失效时，中继返回 403，消息告诉用户去连接或重新授权 GitHub。对这个仓库有没有 push 权限由 GitHub 自己判断：user token 本来就只有用户本人的权限。GitHub 拒绝 token 本身（401）时，中继删掉存着的 token，让用户重新连接。

### 4.4 允许什么

只有**指向这个对话的仓库**、并且在允许列表里的请求才会带上用户 token。其他请求原样匿名转发（去掉 `Authorization`），所以 agent 仍能读公开仓库和公开页面，只是不带任何人的身份。

git smart HTTP（`github.com/<owner>/<repo>(.git)/...`）：

- `info/refs`、`git-upload-pack`：允许（fetch、pull）。
- `git-receive-pack`：中继先解析 pkt-line 的引用更新命令，再转发数据包。只允许 `refs/heads/*`；拒绝删除分支、推 tag 和其他引用。允许 force push，因为 agent 改写自己的分支（rebase）是正常操作。推到哪个分支不在中继里限制，交给 GitHub 的分支保护，和本地一样。

REST（`api.github.com`）：

- `/repos/<owner>/<repo>/...` 下的读请求，以及 PR、issue 评论、review、review 评论、check、status、Actions 运行记录、合并 PR 的写请求。
- 拒绝仓库管理类接口：设置、webhook、协作者、部署密钥、分支保护、Actions secrets 和变量、删除仓库、转移仓库。
- `GET /user`、`GET /rate_limit` 允许（`gh` 用它们确认身份）。

GraphQL（`api.github.com/graphql`）：**第一版拒绝**，返回 403 并提示改用 REST（`gh api repos/{owner}/{repo}/...`）。GraphQL 可以通过 node ID 访问任意仓库，按仓库限制它需要解析查询并逐个核对 node ID，第一版不做。代价是 `gh pr create`、`gh pr view`、`gh pr checks`、`gh pr merge` 这类基于 GraphQL 的子命令不能用；平台说明（第 6 节）告诉 agent 用对应的 `gh api` REST 调用。Claude Code cloud 的 GitHub proxy 也是这样做的。以后如果要支持，就只放行 `gh` 实际发出的那几种查询，并按仓库核对它们的参数和 node ID。

### 4.5 PR 记录

中继看到 `POST /repos/<owner>/<repo>/pulls` 返回成功时，把这个 PR 记到新表 `agent_thread_pull_requests`（对话、轮次、仓库、编号、URL、head 和 base 分支、创建时间）。界面上的 PR 链接、"done" 状态、Watch PR 都从这里取，替代原来由发布流程写入的 `published_pr_url`。用户自己在 GitHub 上开的 PR 不在记录里；需要时用户把 PR 链接贴进对话即可。

## 5. sandbox 里的配置

- **git remote。** 工作区加上 `origin = https://github.com/<owner>/<repo>.git`，不带任何凭据，agent 用平常的 `git push origin <分支>`、`git fetch origin`。
- **凭据和变量。** credential helper 用只在调用时生效的 `GIT_CONFIG_*` 变量配置，不写进共享的 `.git`；`GH_TOKEN` 是中继占位凭据，`GH_REPO` 是对话的仓库。
- **commit 署名。**
  - `GIT_AUTHOR_NAME` / `GIT_AUTHOR_EMAIL` 和 `GIT_COMMITTER_*` 设成用户的 GitHub 名字和 noreply 邮箱 `<id>+<login>@users.noreply.github.com`，来自 `agent_github_accounts`。
  - agent 在 commit message 末尾写 `Co-Authored-By: FreeInference <bot 邮箱>`。bot 邮箱是 `<bot 用户 id>+<app-slug>[bot]@users.noreply.github.com`，GitHub 能把它和 App 的头像对应起来。Claude 运行时用成员私有配置里的 `attribution` 设置自动加；其他四个运行时由平台说明要求 agent 自己加上（见第 6 节）。
- **签名 commit。** sandbox 里没有用户的签名密钥。要求签名 commit 的分支保护会拒绝这些 push，这是已知限制。
- **已有工作区。** `origin` 在新建工作区时记录；这一版之前创建、一直保留的工作区没有它，平台说明告诉 agent 缺了就自己加上。
- **CI 文件。** push 修改 `.github/workflows/` 需要 App 的 `workflows` 权限。第一版不申请，这类 push 会被 GitHub 拒绝；以后要开再单独决定。

## 6. 给 agent 的平台说明

平台原来不给 agent 任何平台级说明。新增一段简短说明（`cloud_agent_host/platform_instructions.py`），只在成员有仓库和中继占位凭据时才给，五个运行时都给，都不写进共享工作区（见本节末尾）。内容：

- 这个工作区的 `origin` 是什么仓库，`git push`、`gh api` 以用户本人的身份生效，只能写这个仓库。
- commit 作者已经设好，不要改；commit message 末尾加 `Co-Authored-By` 那一行。
- ~~用户要求开 PR 时就开，不要反问，也不要让用户去点按钮；先读仓库自己的 AGENTS.md / CLAUDE.md / CONTRIBUTING.md 并跑它们要求的检查，在 PR 描述里写清改动、原因、跑了什么、哪些没跑成。~~
- ~~新建分支，不要直接推默认分支；PR 默认开成 draft。~~

  *2026-11-05 删除：* 这两条是平台替用户定的工作方式，不是 agent 发现不了的事实。怎么做、PR 怎么开，由用户和仓库自己说了算，和同一个 CLI 在用户本机上一样。代价是下面说的：仓库的 CLAUDE.md 不会自动加载，agent 不一定会自己去读。
- `gh` 的 GraphQL 子命令不可用，附上开 PR、看 PR、看检查、合并分别用的 `gh api` REST 调用。

**仓库自己的设置不会自动生效。** Claude 运行时是用 `--setting-sources user` 启动的：共享工作区里仓库的 project/local 设置被当作不可信的启动输入，所以仓库的 `.claude/settings.json` hook（比如本仓库的 pre-PR 检查）和仓库 CLAUDE.md 都不会自动加载。这原本是平台说明要明确叫 agent 自己去读并照做的原因；2026-11-05 起平台不再这样要求，接受 agent 不一定会去读。

**五个运行时都拿到平台说明。** 注入方式各不相同，由 `with_platform_instructions` 统一处理：

- Claude Code 和 pi：`--append-system-prompt`。
- Codex：在 `app-server` 前用 `-c` 覆盖 `developer_instructions` 配置项（已在 0.145.0 二进制的配置 schema 里核对过）。
- OpenCode 和 Kilo：`deploy/sandbox/{opencode,kilo}-freeinference` 包装脚本从 `<NAME>_FREEINFERENCE_INSTRUCTIONS` 读出说明，写成文件，加进它们配置里的 `instructions` 文件列表。

co-author 那一行：Claude 通过 `attribution` 设置自动加（写进成员私有的 settings，改成平台的 co-author）；其他四个运行时没有对应设置，由平台说明要求 agent 在 commit message 末尾自己加上。

## 7. 删除和改造

**删除**（约 6,200 行后端代码、约 7,900 行只测这套的测试）：

- `publish_worker.py`、`publisher.py`、`pr_titles.py`、`storage/_sql/snapshot_title_context.py`。
- 共享快照捕获：`workers/snapshots.py`、`workers/background_wake.py`、`ports/snapshot.py`、`storage/repositories/snapshot.py`，以及 lifespan 里的接线。
- `POST /threads/{id}/publish`。
- sandbox manager 的 `/snapshot`、`/import-base` 端点及其客户端，`base_bundle_request.py`、`base_bundle_stream.py`、`environment_snapshot.py`（名字像环境快照，实际只服务共享快照，`AGENT_SECRET_PATTERN_*` 也随它去掉），supervisor 的 `capture_snapshot` 和 `snapshotting` 屏障，`SNAPSHOT_BASE_MISSING`。
- `patch_gate.py` 整个删除；runner 还要用的 `branch_name_for` 挪到 `cloud_agent_shared/git/branches.py`。
- 设置 `AGENT_GITHUB_TOKEN`、`AGENT_PUBLISH_BASE_BRANCH`、`AGENT_GIT_AUTHOR_NAME`、`AGENT_GIT_AUTHOR_EMAIL`，以及发布和快照循环的各项间隔与超时；Admin 设置里不再有 Publication 分组。
- sandbox 的 installation token（`_claim_credentials.py` 的第二个 token 和 `resident_transport.py` 的 `GH_TOKEN` 注入）。runner 克隆用的只读 token 保留。
- 前端：Create PR / Update PR 按钮、发布状态和提示、`publish.*` 文案。

**保留：**

- `branch_name_for`：runner 用它给工作区建本地分支。
- `import_base_bundle` 和 runner 的 `_import_retained_base`：续接对话时导入 base 用。
- `github_app.py`：克隆 token、分支、`resolve_ref` 和仓库授权都要用。
- 环境快照（`agent_environment_snapshots`）：和发布无关。
- `agent_jobs.published_pr_url`：中继把这一轮开的 PR 写进去。

**有意留着、以后再清理的：**

- 发布流程的表和列：`agent_sandbox_snapshots`、`agent_snapshot_jobs`、早已没人读的 `agent_pr_archive_checks`，它们的触发器和函数（包括 `publication` 推送用的 `agent_sandbox_snapshots_publication_changed`），`agent_sandboxes`、`agent_thread_sandbox_memberships`、`agent_jobs` 上唤醒快照循环的触发器，以及只有发布流程写的 `agent_jobs.published_commit_sha`。这一版的代码既不读也不写它们，由下一版的迁移删除（见第 9 节）。
- `agent_sandboxes.snapshot_state`、`published_base_sha` 两列，以及读它们的守卫。没有东西再写它们，`snapshot_state` 永远是 idle，守卫不会再拦任何操作。
- `agent_model_connection_scan_keys`：绑定 residency 时仍然写入扫描用的密钥副本，但快照扫描删掉以后已经没有读取方，成了只写的表。停止写入和删表放到后续 PR。
- `agent_attempts`、`agent_attempt_ledger` 上的 `ledger` 后台唤醒触发器：监听它们的 `background_wake.py` 已经删掉，触发器只是空发通知。

**改造：**

- `workspace_pr_url`、`published_pr_url` 的读取方（`_sql/sandbox.py`、`_sql/sessions.py` 的 done 状态、`adapt.ts`、`recordedTurnOutputs.ts`、`sidebarRecordChanges.ts`、`RunDetails.tsx`、`execution_queries.py`、`threads.py`）改读 `agent_thread_pull_requests`。job 的 `published_pr_url` 是这一轮开的 PR，`workspace_pr_url` 是对话最新的 PR。Fork 不继承原对话的 PR 记录。
- 停止或删除对话时等待快照的检查（`_GROUP_WORK_PENDING_SQL` 里的快照条件、`publication_pending`）删掉。`agent_sandboxes.snapshot_state` 也不再被当成"忙"：这一版没有任何代码会把它从 `pending`、`snapshotting`、`importing` 改回来，升级时正处在这些状态的组如果还按忙处理，就永远停不下、删不掉，也不会被空闲回收。
- 续接对话的 base 不再随发布前移，每轮都用对话最初的 base；工作区 diff 视图对比的是整个对话的改动。
- Watch PR 的提示词去掉"sandbox 没有凭据"和"等用户点 Update PR"，改成 agent 自己用 git 和 `gh api` REST 调用读 PR、push 到 PR 分支、合并；它的 `require_idle` 不再检查快照状态。Code review 的提示词保留"用本地 git 审查"，去掉"没有凭据"。

## 8. 放弃的东西

- **补丁闸门的检查。** 推送不再经过平台的 `.github/` 检查和密钥扫描。改 workflow 由 App 没有 `workflows` 权限挡住；密钥由 GitHub 的 push protection 和仓库 CI（本仓库有 Gitleaks）兜底。中继以后可以在 receive-pack 里扫描用户自己已知的密钥（模型 key、环境 Secrets），第一版不做。
- **"只有主人点了才发布"。** 发布权回到对话本身：用户说开就开，和本地一样。
- **没有用户 GitHub 连接的仓库不能开 PR。** 部署白名单里匿名克隆的公开仓库仍然能跑任务，但 push 需要用户连接 GitHub。

## 9. 交付

一个 PR（#1103）：本文；GitHub 中继 listener、隧道、hosts 和 CA；中继 token；用户署名和 co-author；五个运行时的平台说明；`agent_thread_pull_requests` 和它的写入；去掉 sandbox 的 installation token；第 7 节的删除和改造。这个 PR 只加 `0112_thread_pull_requests` 一个迁移，是普通的滚动部署。

**删表放到 #1103 之后的下一版**，用单独的迁移做。原因是滚动升级：迁移在替换 API 之前执行，上一版每次读 job 都要读 `agent_jobs.published_commit_sha`，它的发布流程也要用快照表，同一版里删表会让还在运行的上一版出错（CI 的 `scripts/check_rolling_upgrade.py` 会拦下）。所以这一版只是不再读写它们；回滚到上一版不需要降级，也不丢任何东西。等下一版上线时，已经没有会读这些表的版本在运行，再删表和列。

GitHub 上要手动做的：App 改名（生产 `FreeInference`，staging `FreeInference Staging`），换成终端图标头像；如果 slug 跟着变，同步更新 `AGENT_GITHUB_APP_INSTALL_URL`。

## 10. 验证

- **单元和集成测试：** 中继的凭据校验和 fence；仓库范围；允许列表；receive-pack 命令解析（删除、tag、非 `refs/heads/` 都被拒绝）；GraphQL 拒绝；匿名转发去掉 `Authorization`；PR 记录；user token 刷新；未连接 GitHub 时的 403。
- **真实 Kata：** 在 staging 新开一个对话，让 agent 改一个文件、commit、push、开 draft PR。确认：PR 作者是用户，commit 作者是用户、co-author 是 FreeInference，`agent_thread_pull_requests` 有记录，界面显示 PR 链接。再确认 `git push --delete`、推 tag、访问别的私有仓库都被拒绝。测完关掉测试 PR、删掉分支。
