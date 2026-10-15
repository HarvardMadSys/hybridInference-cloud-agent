# 按需发布：只在用户要求时推送分支和开 PR

日期：2026-10-15。状态：方向已确认（第 3 节）；实现和本文在同一个 PR 里。

设计核对基线：远端 `dev`，提交 `6740e229`。文中的行号都按这个提交。

相关：[原生会话与共享工作区](2026-08-31-native-session-fork-shared-workspace-design.md)、[会话连续性架构 §10](2026-09-01-agent-session-continuity-architecture.zh.md)、[从空白开始](2026-10-13-temporary-workspace-design.zh-CN.md)、[对话归档与永久删除](2026-10-13-chat-archive-and-permanent-deletion-design.zh-CN.md)。

## 1. 结论

平台不再主动推送。每轮结束时只结算这一轮，不推分支、不开 PR。用户点 **Create PR** 时，平台才把工作区的改动推到 GitHub 并开一个 draft PR；之后按钮变成 **Update PR**，用户每点一次，才把新的改动推到同一个 PR。

实现上复用现有的发布流水线，只改两处：

1. 每轮结束不再排快照。
2. 新增 `POST /v1/agent/threads/{thread_id}/publish`：用户点按钮时在这里排快照。之后的抓取、扫描、推送、开 PR 和 base 导入都不变。

不需要数据库迁移。

## 2. 背景

### 2.1 原来的行为

绑定仓库的对话，每一轮只要改了文件，结束后平台就会自动：

1. 抓取工作区相对上次发布点的完整 diff；
2. 做密钥扫描和路径检查；
3. 推到 `agent/<对话id>` 分支；
4. 开一个 draft PR，或者更新已经开着的那个。

这个过程没有开关，用户不点任何按钮也会发生。

### 2.2 为什么要改

- 用户没要求，平台就往用户的仓库推分支、开 PR，等于替用户做了决定。探索性的对话也会在仓库里留下 `agent/*` 分支和 draft PR；仓库如果配置了 PR CI，每轮还会触发一次。
- 同类产品都由用户触发：Claude Code on the web 做完后显示 **Create PR** 按钮；Cursor 从界面发起的任务，做完后由用户选择开 PR 或拉到本地；Codex 云端版由用户点 **Create PR**。
- 自动发布失败后没有手动重试入口，只能等用户再发一轮。改成按钮后，失败了再点一次就行。

## 3. 已确认的决定

1. 每轮结束都不推分支、不开 PR。
2. 开过 PR 之后，后续轮次也**不**自动更新 PR，每次更新都要用户点 Update PR。规则只有一条：平台从不主动推。
3. 已有的对话（包括已经开着 PR 的）上线后按同一规则走。
4. agent 在 sandbox 里用 `GH_TOKEN` 自己推送，不在本设计范围内，行为不变。
5. 用按钮，不靠在对话里让 agent 开 PR。按钮走平台的密钥扫描；让 agent 自己推的路照样存在。

## 4. 用户体验

- **按钮位置**：对话最新一轮的结果区（[`RunOutcome.tsx`](../../../apps/frontend/src/components/features/agents/job-detail/RunOutcome.tsx)），和 View changes、Open draft PR 并排。工作区面板的 Git 标签页本次不放。
- **按钮**：一个按钮，两种名字。工作区还没推送过时叫 **Create PR**；推送过以后叫 **Update PR**。平台不跟踪 PR 是否已被合并或关闭；如果之前的 PR 已经合并或关闭，点 Update PR 会像以前一样开一个新的 draft PR。
- **什么时候能点**：工作区里没有正在运行的轮次，也没有正在进行的推送，并且看的是最新一轮。轮次运行中不显示按钮；推送进行中按钮置灰。
- **状态提示**（按钮下方一行字）：推送中显示「正在推送改动…」；失败显示「推送失败，请重试」，按钮恢复可点；有已结束但没推送的轮次时显示「有改动还没推到 PR」；推送时发现没有任何改动，显示「没有需要推送的改动」。失败的具体原因和以前一样，写进对话的事件里。
- **Open draft PR、Review、Watch PR and merge**：最新一轮自己没有 PR 链接时（比如它还没推送），用工作区最近一次推送的 PR 链接。
- **Fork 共享工作区**：同一个工作区里的几个对话共用一个 `.git` 和一条发布线，这是现有设计。在任何一个对话里点按钮，推的都是整个工作区，目标是源对话的分支 `agent/<源对话id>`。

## 5. 技术设计

### 5.1 术语和原来的流水线

- **sandbox group**：一个工作区容器。Fork 出来的对话和源对话在同一个 group 里，共用一份工作区。
- **workspace epoch**：group 的修改序号。每结束一轮加一，现在每次发布请求也加一。
- **快照（snapshot）**：`agent_sandbox_snapshots` 的一行，记录「某个 epoch 的工作区相对 `published_base_sha` 的 diff」，状态依次是 `pending → capturing → captured → publishing → published/failed`。
- **base 导入**：推送成功后，把推上去的 commit 导入容器的 `.git`，但不改 HEAD 和文件，让下一次 diff 以它为起点。

原来一轮结束时，`settle_thread_sandbox`（[`sandbox.py:1573`](../../../apps/backend/cloud_agent/storage/repositories/sandbox.py#L1573)）会：

1. 把 group 的 epoch 加一；
2. 如果对话绑定了仓库（`publishes`，[`sandbox.py:1616`](../../../apps/backend/cloud_agent/storage/repositories/sandbox.py#L1616)），把 group 的 `snapshot_state` 设成 `pending`，并打开或扩展这个 group 唯一的 `pending` 快照，把这一轮记进 `agent_snapshot_jobs`（[`sandbox.py:1798`](../../../apps/backend/cloud_agent/storage/repositories/sandbox.py#L1798)）。

之后由三个后台步骤接手：

- 抓取协调器在 group 空闲时抓取 diff（[`snapshot.py:99`](../../../apps/backend/cloud_agent/storage/repositories/snapshot.py#L99)）。diff 为空时，快照直接记为 `published`，不开 PR（[`snapshot.py:254`](../../../apps/backend/cloud_agent/storage/repositories/snapshot.py#L254)）。
- 发布 worker 做扫描、推送、开 PR（[`publish_worker.py`](../../../apps/backend/cloud_agent/publish_worker.py)）。
- 发布后做 base 导入（[`publish_worker.py:483`](../../../apps/backend/cloud_agent/publish_worker.py#L483)）。

### 5.2 改动一：每轮结束不再排快照

`settle_thread_sandbox` 去掉了 `publishes` 分支，对所有对话都走临时工作区原来的路径：epoch 照样加一，`published_base_sha` 照样记录，但不碰 `snapshot_state`，也不建快照。抓取和发布 worker 都看不到这一轮。

这一轮只是还没进入任何快照，等用户点 Create PR / Update PR 时再被收进去。

### 5.3 改动二：`POST /v1/agent/threads/{thread_id}/publish`

路由在 `api/jobs.py`，风格沿用 `/threads/{thread_id}/goal/pause`；存储方法是 `SnapshotRepository.request_snapshot_publication`。调用者必须是这个对话的所有者。接口立即返回 202，抓取和推送在后台继续。

一个事务里依次做：

1. 锁住对话（必须属于调用者、未删除）。临时工作区返回 `publication_unavailable`。对话还没跑过轮次（没有 group）返回 `nothing_to_publish`。
2. 锁住 group。已经有进行中的快照（`pending/capturing/captured/publishing`）时，直接返回它，`requested: false`，不新建，所以连点两次是安全的。
3. 拒绝：group 里有正在运行的轮次或正在进行的 Fork，返回 `turn_running`；group 正在删除、已删除，或不在 `running/stopped` 状态（比如 host 下线后等待恢复的 `cold`），返回 `workspace_unavailable`。
4. 找出要收进快照的轮次：这个 group 所有对话里，已结算（`native_turn_no` 不为空）、已结束、还没进过任何快照的轮次。
   - Fork 时复制到子对话里的源轮次（`fork_source_job_id` 不为空）不算：它们没在这个工作区里运行过。
   - 每个对话只取它最后一个进过快照的轮次之后的轮次，把发布改成快照之前的旧轮次排除在外。
   - 一个都没有时，只收 group 里最新结束的那一轮（它已经在以前的快照里；`agent_snapshot_jobs` 的主键是 `(snapshot_id, job_id)`，同一轮可以出现在多个快照里）。这覆盖了失败后重试，以及只在终端里改了文件的情况。
5. 对这些轮次加 `FOR KEY SHARE NOWAIT` 锁（见 5.5），拿不到锁就返回 `workspace_busy`，什么都不改。
6. 把 group 的 epoch 加一，`snapshot_state` 设成 `pending`（当时是 `snapshotting/importing` 则保持不变）。在新 epoch 上插入 `pending` 快照：anchor 是收进来的最新一轮，`base_sha` 是 group 的 `published_base_sha`。`agent_snapshot_jobs` 按结算顺序写入，每行时间戳依次加 1 微秒，因为发布时按这个时间读取各轮的摘要和消息。
7. 插入快照时，数据库触发器（migration 0032）会唤醒抓取协调器，不需要额外调用。

之后的抓取、扫描、推送、开 PR、base 导入、失败重试（5 次）、`dirty` 处理全部不变。抓取发生在点击之后，所以用户在工作区终端里手动改的文件也会被推上去。

**为什么每次点击都新开一个 epoch。** 一条规则同时覆盖「第一次推」「有了新轮次再推」和「失败后重试」，不用把失败的快照改回 `pending`：

- 失败的快照保持 `failed`，符合现有的「失败的快照不再被领取」约定。新快照是相对上次成功发布点的完整 diff，自然包含失败那次的改动。这和原来「下一轮的快照取代失败的快照」是同一个机制，只是触发者从「下一轮结束」变成了「用户点击」。
- 如果失败发生在分支已推送、PR 还没开成的时候，那次推送的提交记在失败快照的 `published_commit_sha` 上，新快照被领取时会通过 `own_pushes` 拿到它，在它之上继续，不会被当成「分支被别人改过」。如果把失败的快照改回 `pending` 重新抓取，抓取会把 `published_commit_sha` 清掉（[`snapshot.py:254`](../../../apps/backend/cloud_agent/storage/repositories/snapshot.py#L254) 之后的更新），重试就会被拒。
- epoch 只用作快照的顺序号：host 端只在抓取回执里原样带回它（[`sandbox_manager.py:3314`](../../../apps/backend/cloud_agent_host/sandbox_manager.py#L3314)），不做解释；结算的幂等判断看的是 `last_settled_job_id` 等字段，不看 epoch。

### 5.4 为什么不选「每轮照样建快照，只是先不发」

看起来改动更小，但不可行：group 的 `snapshot_state = pending` 和进行中的快照会挡住以下操作。

- **空闲停止**：sandbox 空闲停止会被判为 `busy`（[`sandbox.py:37`](../../../apps/backend/cloud_agent/storage/repositories/sandbox.py#L37) 的 `_GROUP_WORK_PENDING_SQL`，以及停止、迁移、删除前的 `snapshot_state` 检查）。
- **删除对话**：会被判为 `publication_pending`（[`_sql/jobs.py:112`](../../../apps/backend/cloud_agent/storage/_sql/jobs.py#L112)）。

用户不点按钮，快照就一直停在 `pending`，容器永远停不下来，对话也删不掉。要让那个方案可行，就得加新的快照状态、改约束、写迁移，还要逐个改上面这些检查。按需建快照不需要这些。

### 5.5 加锁顺序

`cloud_agent/storage/__init__.py` 规定的顺序是：对话 → job → membership → group → 待处理快照，外键也算加锁。请求要先锁 group 才能知道收哪些轮次，而插入 `agent_snapshot_jobs` 会通过外键对这些轮次加共享锁，等于在 group 之后锁 job，和顺序相反。

follow-up 正是拿着最新一轮再去等 group 的写入方，如果请求在 group 之后等那一轮，两边会卡死。所以请求在 group 之后用 `FOR KEY SHARE NOWAIT` 先锁这些轮次：拿不到就立刻放弃、交还 group，用户看到「稍后重试」。这属于 `storage/__init__.py` 允许的例外：带 `NOWAIT` 的锁从不等待，因此可以不按顺序。`KEY SHARE` 和发布记录、状态变更用的 `NO KEY UPDATE` 不冲突，只和少数显式的 `FOR UPDATE` 冲突，所以平时不会触发。

### 5.6 新增的状态字段

job 响应里新增三个字段，都描述整个工作区：

- `workspace_unpublished_changes`：有已结算、还没进过任何快照的轮次（Fork 的复制轮次除外；每个成员只看它最新结算的一轮，因为一次请求会收进之前所有的轮次），或者最近一次推送失败。临时工作区总是 `false`。
- `workspace_pr_url`：工作区最近一次推送到的 PR 链接。
- `workspace_publication_unchanged`：最近一次推送发现没有任何改动，所以没开 PR。

### 5.7 上线时的存量

- 上线前已经排好的快照照常发布完，不受影响。抓取期间某轮结算时打开的「旧式」待处理快照，抓取被退回时照旧并进去（`_fold_into_pending_snapshot` 保留，测试用手工构造的旧式快照覆盖）。
- 上线后，已有 PR 的对话不再自动更新 PR，用户点 Update PR 才更新。
- 上线前结束的轮次已经进过快照，不会被再次收进去。

## 6. 受影响的功能

| 功能 | 影响 | 处理 |
| --- | --- | --- |
| View changes / Git 面板 | 读的是容器里的实时工作区，和发布无关 | 不变 |
| Open draft PR / Review | 最新一轮可能还没推送，自己没有 PR 链接 | 退回到工作区最近一次推送的 PR 链接 |
| Watch PR and merge | ① 提示词原来让 agent 修完后「结束这一轮，平台会发布改动」（[`watchPullRequest.ts:14`](../../../apps/frontend/src/components/features/agents/watchPullRequest.ts#L14)），上线后不再成立。② 发起时的守卫（`require_idle`，[`pending_messages.py:303`](../../../apps/backend/cloud_agent/storage/repositories/pending_messages.py#L303)）只检查「最新快照已发布」；没推送的轮次不产生快照，守卫会放过一个落后于工作区的 PR | ① 提示词改成：修完后结束这一轮，请用户点 Update PR，再点 Watch PR and merge。② 守卫加一条：有未推送的轮次时返回 `unpublished_changes`；前端在这种情况下也把按钮置灰 |
| PR 合并后自动归档 | 发布时登记检查。有更新的未发布轮次时，原有逻辑本来就不会归档 | 不变 |
| Goals | 自动执行的轮次以前每轮都更新 PR | 不再自动推，由用户决定什么时候推 |
| 删除对话 | 只有推送进行中时才会被 `publication_pending` 挡住，时间很短 | 不变 |
| 临时工作区 | 本来就不发布 | 不变，不显示按钮，接口返回 `publication_unavailable` |
| agent 自己推送 | 不在范围内 | 不变 |

## 7. 没推送的改动存在哪里

没推送的改动只在这个对话的 sandbox 容器里。

- 空闲时容器会停止，但工作区保留；下次对话或推送时再启动。
- 容器只有在用户删除对话（并且是 group 里最后一个对话）时才会被删除。归档不删除容器。
- 部署配置了对象存储时，容器每次正常停止都会保存一个 checkpoint（#609）；host 离开资源池时，已停止的 group 可以从 checkpoint 在别的 host 恢复（#658）。

和以前相比的区别：以前每轮都推到 GitHub，相当于每轮自动备份了一次；现在在点按钮之前，GitHub 上没有这份改动。容器运行中 host 意外损坏，或者部署没有配置对象存储时，没推送的改动可能丢失。这和本地使用 Claude Code 不 push 时一样。界面上用「有改动还没推到 PR」提示用户。

注：快照表里的 `patch_content` 只给发布流水线用（全仓库只有 `snapshot.py` 读写它），不是独立的备份，所以不建快照不会少掉别的保护。

## 8. 文档

- `README.md` 的 Pull requests 条目、`AGENTS.md` 第 1 节：改成在所有者要求时发布。
- `docs/developer/operations.md` 的 Resume, Fork, and publication 一节：发布由用户触发，失败后由用户重试。
- `gateway_contracts/agent-control-plane.openapi.json`：新接口、`AgentThreadPublicationResponse` 和 job 的三个新字段。
- 用户文档在发行版里（FreeInference 的 `agents.md`），在这个改动进入生产版本后再更新。

## 9. 测试

后端（`make test-db`）：

- `tests/integration/storage/test_publication_request.py`：轮次结束不建快照，不挡空闲停止和删除；请求打开一个快照，进行中再请求返回同一个；推送失败后工作区算「有未推送的改动」；运行中、非所有者、不存在、已删除、未开始、正在删除、临时工作区都被拒绝。
- `tests/integration/storage/test_snapshot_supersession.py`（按新语义重写）：用户的下一次请求取代失败的快照；没有新轮次时重试会新开一个 epoch；重试接上失败那次已推送的提交（`own_pushes`）；推送进行中结算的轮次要等用户再请求；旧的 epoch 不会在新的之后发布。
- `tests/integration/storage/test_lock_order.py`：新增「请求在 group 之后不等被锁住的轮次」；两个 fold 测试改用手工构造的旧式快照。
- `tests/integration/servers/test_pending_message_queue.py`：通过 HTTP 发布、重复请求、Fork 的复制轮次不被收进去、各种拒绝的错误码；Watch PR 在有未推送轮次时返回 `unpublished_changes`。
- `tests/integration/storage/test_push_usage_and_workers.py`：轮次结束不唤醒抓取，用户的请求唤醒抓取。
- 其他依赖「每轮自动建快照」的测试，改为在轮次结束后先发请求。

前端（`make ci-frontend`）：按钮的出现条件、置灰、两种名字、点击后调用接口并刷新、拒绝原因的显示、推送中和「没有改动」的提示；字段映射；API 客户端。

## 10. 风险与待定

- **请求之后才开始的轮次**：抓取要等工作区空闲，如果请求之后、抓取之前又跑了一轮，这一轮的改动会被推上去，但它不算进这次快照。按钮会继续提示「有改动还没推到 PR」，再点一次会显示「没有需要推送的改动」并清掉提示。代价只是多点一次，没有改动会丢。
- **PR 创建失败的分类**：#663 把创建 PR 时除「PR 已存在」以外的 422 都判为永久失败，其中包括刚推完分支、GitHub 还没看到新分支时返回的 `head invalid`。改成按钮后用户可以重试，代价变小，但仍建议 `head invalid` 保持可重试。
- **未推送提示的精度**：「有改动还没推到 PR」按轮次判断，不检查文件是否真的有改动；反过来，只在终端里改了文件、没有新轮次时也不会提示。点按钮总能拿到准确结果。以后要更准确，可以改成读 Git 面板的实时状态。
