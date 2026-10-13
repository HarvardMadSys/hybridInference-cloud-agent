# Cloud Agent 对话归档与永久删除设计

日期：2026-10-13。状态：与同一 PR 中的实现一致。

设计核对基线：远端 `dev`，提交 `7a98b2ac`（已包含 #597，即删除聊天后回收 resident slot）。

目标：把“归档”和“永久删除”做成两个各自独立、用户都可以直接操作的功能。

- **归档**：从对话列表中隐藏，可以恢复。
- **永久删除**：用户再也无法查看或恢复这个对话。平台的数据库记录永久保留，只对用户隐藏。删除时会停止这个对话的任务和进程，并吊销它的推理凭据。如果它是所在 sandbox 的最后一个对话，sandbox 也会被删除，释放 host 资源。

## 一 原有行为与问题

### 用户能做的操作

| 操作 | 入口 | 原有实现 |
| --- | --- | --- |
| 归档 | 对话列表 | `POST /sessions/archive` 设置 `agent_threads.archived_at`。同时暂停 goal、取消排队中的 goal 轮次，并暂停待发送消息队列。可以恢复。 |
| 删除 | 只在归档列表中 | `POST /sessions/delete` 调用 `ConversationCommands.delete_sessions`。对话必须已归档；有任务在运行、进程正在启动、Fork 正在进行或 PR 正在发布时会被拒绝。 |

`delete_sessions` 是软删除：它只设置 `agent_threads.deleted_at`，并硬删除上传附件和待发送消息；任务、事件、消息、执行记录、成员关系、residency 和 sandbox 历史全部保留。本设计继续沿用软删除，数据库记录保持不变。

### 问题

1. **删除后进程和凭据仍在。** 删除不会停止这个对话在 sandbox 里的 Agent CLI 进程，也不会释放它的 residency。只要同一个 sandbox 里还有其他对话在活动，这个进程就会一直运行，推理凭据也会随 host 心跳一直续期，直到整个 sandbox 空闲停止。
2. **已删除对话的 sandbox 从不删除。** sandbox 里所有对话都被删除后，它的容器和工作区仍然一直占着 host 磁盘。
3. **删除前必须先归档、必须等任务结束。** 用户想永久删除一个活跃对话时，要先归档，还要等运行中的任务结束。
4. **文案和实际行为不一致。** 删除弹窗写的是“从你的会话和历史记录中永久移除”，但平台会保留数据，这个说法容易让用户误解。

## 二 已确定的产品决定

| 问题 | 决定 |
| --- | --- |
| 永久删除要不要先归档 | 不需要。对话列表和归档列表都可以直接永久删除，用确认弹窗防止误删。 |
| 要不要撤销期或回收站 | 不要。想保留就归档；永久删除对用户立即生效，不可恢复。 |
| 数据库记录 | 永久保留，只对用户隐藏。管理员仍然可以查看，界面上标注 “Deleted by user”。 |
| sandbox | 被删除的对话如果是所在 sandbox 的最后一个未删除对话，就删除这个 sandbox。开了 S3 归档就先归档再删，没开就直接丢弃。还有其他对话在用的 sandbox 不删。 |

## 三 用户可见行为

| | 归档 | 永久删除 |
| --- | --- | --- |
| 可以恢复 | 可以 | 不可以 |
| 入口 | 对话列表 | 对话列表（单个）、归档列表（单个、按项目、按筛选结果、全部） |
| 运行中的轮次 | 不受影响；归档后 goal 暂停 | 排队中的被取消，运行中的被要求停止 |
| Agent 进程和推理凭据 | 不变；空闲后照常停止 | 立即吊销凭据，并通知 host 关闭进程 |
| 用户能否看到 | 在归档列表中可见 | 任何地方都不可见 |
| 平台数据库记录 | 保留 | 保留 |
| sandbox | 保留 | 如果是最后一个对话就删除，否则保留 |

确认弹窗的文案：

- 写“你将无法再查看或恢复这些会话，正在运行的任务会被停止”，不再写“从记录中永久移除”。
- 说明已经 Fork 出去的对话、已经推送的分支和 PR 不受影响，与其他会话共用的工作区会保留。

隐私政策需要写明：用户删除对话后，平台会保留相关数据。这一条不在本仓库内，需要另行同步。

仍然拒绝删除的情况只剩两种：Fork 正在进行（`fork_in_progress`），以及这个对话的轮次正包含在一个待发布的工作区快照里（`publication_pending`）。两者持续时间都很短，提示用户稍后重试即可。原来的 `not_archived` 和 `session_active` 已经删除。

## 四 删除请求

`ConversationCommands.delete_sessions` 在一个事务里完成以下步骤。加锁顺序是 thread → 成员关系，与 Fork 准备一致。

1. 锁住所选对话，拒绝 `fork_in_progress` 和 `publication_pending`。
2. 设置 `deleted_at = NOW()`。已有的 `deleted_at IS NULL` 检查会拒绝这个对话之后的新轮次、Fork 和归档操作。
3. 取消排队中的任务：`state = 'cancelled'`，写法与取消 goal 轮次的 `CANCEL_QUEUED_GOAL_ROUNDS_SQL` 相同。这一步是必需的：`claim_job` 不检查 `deleted_at`，任务如果留在队列里，仍然会被领走执行。
4. 对运行中的任务设置 `cancel_requested`，由 worker 在下一次心跳时完成受 fence 保护的终止。
5. 暂停 goal，暂停原因记为 `cancelled`，说明为“the conversation was deleted”。
6. 删除附件和待发送消息（原有行为）。
7. 释放这个成员的 residency：把 `residency_status` 置为 `'none'`，返回每一代 residency 的 thread、generation、host 和 grant id。

API 路由（`api/catalog.py`）在事务提交后调用 `application/residency.py` 的 `close_released_residencies`，逐个尽力执行：

- 吊销 grant；
- 通过 host 的 sandbox manager 调用 `shared_residency_lost`，让 host 立即关闭这个成员的进程。

这一步失败不影响请求返回。下一次 host 心跳续期时会发现 residency 已失效，在 `residencies_lost` 里带回这一代 residency，host 收到后会调用 `residency_lost` 关闭进程（`api/hosts.py`，`runner._handle_host_residencies_lost`）。

这一步有时间上限，因为用户在等删除结果：

- 各个会话并发处理，吊销和关闭各自最多同时 16 个，两者用不同的并发限制。这样即使 host 不响应、占满了关闭名额，所有 grant 仍然会被吊销。
- 整个步骤最多等 5 秒（`RELEASED_RESIDENCY_CLOSE_TIMEOUT_S`）。单次 sandbox manager 调用本身的超时是 60 秒；没完成的关闭交给上面的心跳兜底。没来得及吊销的 grant 也无法再续期，会在 TTL 到期时失效。

### 删除和正在启动的 worker

删除可以发生在 worker 还在启动、attempt 仍然有效的时候。如果不加限制，这个 attempt 可以在删除之后再建立一代新的 residency，删除时发出的关闭通知只针对旧的一代，新的一代就会一直运行并续期。

因此 `establish_residency` 和 `admit_thread_sandbox_turn` 都会拒绝已删除的对话。检查写在它们最后的 `UPDATE` 语句里，而不是前面的读取里，原因是加锁顺序：

- 删除按 thread → 运行中的 job → 成员关系的顺序加锁；`establish_residency` 按 attempt → job（共享锁）→ 成员关系的顺序加锁。两边都是先 job 后成员关系，并且都不会在持有成员关系锁的同时等待 job 锁，所以不会死锁。
- 如果删除先锁住 job，`establish_residency` 会一直等到删除提交；它最后的 `UPDATE` 使用新的快照，能看到 `deleted_at`，于是拒绝。
- 如果 `establish_residency` 先拿到 job 锁，删除会等它提交；删除释放成员关系时，会连同刚建立的那一代一起释放，并把它交给上面的关闭步骤。

`admit_thread_sandbox_turn` 本来就要求那一代 residency 处于 `hot`，删除之后自然无法通过；`deleted_at` 检查是第二道保护。

### 已知限制

host 在 `residency_lost` 之后会把这个成员的 `residency_generation` 报成 0，而心跳回写成员状态时要求 `residency_generation <= 上报值`。所以进程虽然已经关闭，数据库里这个成员仍显示 `live`，直到整个 sandbox 停止。影响如下：

- 它的 resident slot 要等 sandbox 停止后，才会在下一次 Fork 时由 #597 的逻辑回收；
- 不影响 sandbox 删除，因为删除本来就要等 sandbox 停止。

修正这一点需要改 host 的上报或心跳的回写条件，不在本 PR 范围内。

## 五 删除 sandbox

现有的 sandbox 删除 worker（`workers/sandbox_deletion.py` 的 `coordinate_sandbox_deletions`）每 60 秒额外运行一次 `retire_deleted_session_sandboxes`。

### 候选

`SandboxRepository.sandboxes_of_deleted_sessions` 列出满足以下全部条件的 sandbox，每次最多 20 个：

- `deletion_state = 'none'`，状态是 `running` 或 `stopped`，并且有 `container_id`；
- 所在 host 在线；
- 有成员，并且所有成员对应的对话都已删除。失败 Fork 的占位对话本来就会被标记为已删除。

这个查询不加锁。之后的停止请求和删除请求会锁住 sandbox，再各自检查一遍自己的 fence。

### 运行中的 sandbox：先请求停止

`request_deleted_sessions_stop` 按“所有成员关系 → sandbox”的顺序加锁，再次确认所有对话都已删除、状态是 `running`、generation 和 container 没变，并且没有进行中的工作（成员 attempt、Fork 操作、运行中的任务、待发布快照、`active_operation_count`）。

确认后写入和管理员停止相同的标记：`state = 'stopping'`、`stop_requested_at = NOW()`。同时记录一条 `deleted_sessions_stop_requested` 轨迹事件，`source = 'system'`，`actor_id = 'user-deletion-sweeper'`。host 的下一次心跳会通过 `due_thread_sandbox_stop` 收到停止指令。如果 host 上还有终端或操作没关，host 会拒绝停止，下一次心跳再重试。

如果有运行中的任务（比如刚被要求停止、还没结束），就返回 `busy`，下一轮扫描再试。

### 已停止的 sandbox：发起删除

调用现有的 `request_admin_delete`，`actor_id = 'user-deletion-sweeper'`，`admin_ip = 'internal'`，`source = 'system'`。`source` 是本次新增的参数，原来写死为 `admin`。

| 部署配置 | 处理 |
| --- | --- |
| 开了 S3 归档（`S3SandboxArchiveStore.from_settings().available`） | `discard=False`：先导出到 S3 并校验，再删除 host 上的容器 |
| 没开 S3 归档 | `discard=True`：直接删除容器，不上传 |

之后由现有的删除 worker 执行，复用 host 侧的删除操作、失败重试和审计记录。开了 S3 却在归档时失败的情况（比如超过 5 GiB 上限），沿用现有行为：保留原容器，记录错误，由管理员处理，不会自动降级为直接丢弃。

### 上线影响

上线后，历史上所有对话都已被删除、并且 host 在线的 sandbox，都会被这个扫描逐批停止并删除。这符合本设计的决定，但是一次性的批量操作。上线前应该确认生产环境的 S3 归档配置，并预计删除 worker 在头几个小时的负载。

### 生产环境

代码里 S3 归档默认关闭，需要管理员在 Admin → Settings → Sandbox 配置后才会开启。生产部署应该开启它，这样删除后还留有一份完整快照。这一条建议写进部署文档的上线检查清单，但本功能不把 S3 作为硬性前提，以免影响本地开发和自部署环境。

## 六 管理员视图

- 管理员对话列表和对话详情页原本就会给已删除对话显示标签，文字从 “Deleted” 改为 “Deleted by user”。
- 内容照常可以查看，用量统计不受影响。
- 按删除状态筛选、以及 sandbox 删除后打开工作区时的专门提示，都不在本 PR 范围内。

## 七 API 与前端

后端：

- `POST /sessions/delete` 不再要求对话已归档，也不再因为有运行中的轮次而拒绝；只保留 `fork_in_progress` 和 `publication_pending` 两个拒绝原因。响应格式不变。
- `AgentSessionDeleteBlockReason` 去掉了 `not_archived` 和 `session_active`。
- 没有数据库迁移。

前端：

- 侧边栏每个对话行在“置顶”和“归档”旁边新增“永久删除”图标，点击后弹出确认框。确认框复用归档页的 `ArchiveDeleteDialog`。
- 拒绝原因到文案的映射 `SESSION_ERROR_KEYS` 移到 `ArchiveControls.tsx`，由侧边栏和归档页共用。
- 归档页所有删除入口改名为“永久删除”。
- 中文和英文文案同步更新，去掉了不再使用的 `archive.sessionActive` 和 `archive.sessionNotArchived`。

## 八 测试

- `dbtest`：
  - `test_deleting_a_working_session_cancels_its_turns_and_goal`：删除未归档、有排队和运行中任务的对话，验证任务被取消或被要求停止，goal 被暂停。
  - `test_sandbox_of_deleted_sessions_is_stopped_then_deleted`：分别在有 S3 和没有 S3 时验证：两个对话只删一个时不动 sandbox；最后一个也删掉后请求停止，host 心跳能收到停止指令；停止后发起删除，`deletion_discard` 与 S3 配置一致；轨迹事件来源为 `system`；数据库记录保留。
  - `test_deleting_an_idle_shared_session_keeps_workspace_sibling_and_billing`：改为验证删除会释放 residency，之后的续期返回失效；共享工作区、兄弟对话和计费记录不受影响。
  - #597 的槽位测试改为不先归档直接删除，并验证删除返回已释放的 residency。
  - `test_a_starting_turn_cannot_revive_a_deleted_chats_residency`：删除发生在 worker 启动期间；同一个 attempt 之后既不能建立新的 residency，也不能送达 prompt，被释放的那一代也不能续期。
- API 测试：删除后吊销 grant、通知 sandbox manager 关闭进程；sandbox manager 报错时删除仍然成功；20 个会话的 host 都不响应时，删除仍在时限内返回，所有 grant 都被吊销，同时等待的 host 不超过并发上限。
- 前端 vitest：侧边栏删除需要确认，确认后调用接口、隐藏对话并离开当前页面；被拒绝时在弹窗里显示原因；归档页的文案和拒绝原因同步更新。
