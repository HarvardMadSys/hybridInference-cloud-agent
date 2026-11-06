# A2：用显式状态替换 host 上 sandbox group 的生命周期 flag

日期：2026-11-05。状态：提议，待评审。实现前不改任何行为。

设计核对基线：分支 `murphy/refactor/structural`（PR #1111），提交 `6c930f118`。supervisor 在这个分支上已经拆开，A1 的外观层 `group_lifecycle.py` 已经存在。文中代码路径都相对 `apps/backend/cloud_agent_host/`，行号都是基线上的。

相关：[结构重构](2026-11-05-structural-refactor.zh-CN.md) §5.1、§13.2、§15、§16；[host 协议 v2](2026-11-05-host-protocol-v2.zh-CN.md)；[原生会话、Fork 与共享工作区](2026-08-31-native-session-fork-shared-workspace-design.md)；[推迟初始 archive 上传](2026-11-03-deferred-initial-archive-upload.md)。

## 1. 结论

- **进行中的操作不是一个 Optional，也不是任意集合。** 它是四个各自至多一个的槽：启动、停止、独占工作区、生命周期检查。四个槽两两组合的六对里，有五对可以同时成立（其中"启动 + 检查"只在容器 lost 时出现），只有"启动 + 停止"在正常运行中不可达。所以 §5.1 草图里的 `operations: set[LifecycleOperation]` 改成四个 `Optional` 字段，每个字段记录持有者是谁。
- **`stopping` 有四个持有者，不是一种操作。** 普通停止、reconcile 的收尾、关机时的 archive 重试，以及管理员 archive 请求。最后一个会一直持有，直到 commit 或 abort，比它自己的生命周期检查活得更久。新表示把这四种持有者分开记录，准入仍然把它们都当作 `STOPPING`。
- **`start_failed` 和 `container_lost` 是记录。** `start_failed` 只有一处赋值（`group_lifecycle_controller.py:484`）；`container_lost` 只会被置为真（`shared_session_state.py:343`），从不清除。两者可以和任何进行中的操作同时成立。
- **屏障之间有三条可以证明的蕴含**，可以收成两个记录：`FinalArchive`（`stop_archive_pending`、`stop_archive_blocked`、`stop_archive_members`）和 `OrphanFinalization`（`orphan_finalization_pending`、`orphan_marker_unreadable`）。
- **有三条不变量证明不了**（§4.4），两条可能是现有的竞态。A2 不靠推断去修它们，而是在第 6 节列成待决问题。在决定之前，新表示照样能表示这些状态。
- 迁移分五步（§6）。前三步行为不变；第四步收紧一条只在关机顺序被打破时才可达的准入，单独提 PR；最后加一个棘轮测试，禁止在 `group_lifecycle.py` 之外给这些字段赋值。

## 2. 范围

这里说的字段是 `_Group`（`shared_session_state.py:215`）上的这些：

| 类别 | 字段 |
|---|---|
| 进行中的操作 | `starting`、`stopping`、`snapshotting`、`lifecycle_check` |
| 记录 | `start_failed`、`container_lost` |
| 屏障 | `initial_archive_preservation_pending`、`stop_archive_pending`、`stop_archive_blocked`、`stop_archive_members`、`orphan_finalization_pending`、`orphan_marker_unreadable`、`start_archive_completion_pending` |
| 运行标签 | `start_seq`、`run_unfenced`、`sealed_seq` |
| 相关但不替换 | `archive_request_id`（它是 `stopping` 的一个持有者）、`lifecycle_generation`（fence 时钟） |

不在范围内：成员租约、`operation_tokens`、`terminal_tokens`、`idle_deadline`、`network_proof`。准入时仍然只把它们当作"有没有占用者"来读，和 A1 一样。

所有修改都在 `SharedSessionState.lock` 下进行。这把锁是一个 `RLock`（`shared_session_state.py:317`），它的 `condition` 用来等待转移结束。Docker、marker 和 archive 的 I/O 都在锁外做，做完再回到锁里重新校验。

## 3. 全部赋值

清单是用 AST 扫描整个 `cloud_agent_host/` 得到的：所有 `Assign`、`AugAssign` 中目标属性名在上表里的语句，`setattr`，以及 `_Group(...)` 和 `replace(...)` 的关键字参数。共 122 处，分布在 6 个文件里，测试不计在内：

| 字段 | 次数 | 字段 | 次数 |
|---|---|---|---|
| `stopping` | 23 | `lifecycle_generation` | 6 |
| `stop_archive_members` | 15 | `start_archive_completion_pending` | 5 |
| `stop_archive_pending` | 14 | `initial_archive_preservation_pending` | 4 |
| `orphan_finalization_pending` | 12 | `archive_request_id`、`sealed_seq` | 各 3 |
| `stop_archive_blocked` | 10 | `starting`、`start_seq`、`run_unfenced`、`lifecycle_check` | 各 2 |
| `snapshotting` | 9 | `start_failed`、`container_lost` | 各 1 |
| `orphan_marker_unreadable` | 8 | | |

没有任何一处用 `setattr` 写这些字段。§5.1 里说的"40 处"是拆分前的数字，那时 `stop_archive_*` 和 `orphan_*` 还没有计入。

"锁"一列里，"L"表示在 `with self._state.lock` 块内。"调用方 L"表示函数名以 `_locked` 结尾，或者只在 L 内被调用。"持有者"一列写的是这次赋值发生时，这个调用方已经持有的生命周期资源。

### 3.1 `group_lifecycle_controller.py`

| 行 | 方法 | 锁 | 持有者 | 前 → 后 |
|---|---|---|---|---|
| 146 | `stop_all_for_shutdown` | L | 无；准入 `TAKE_STOP`（只拒 `STOPPING`） | `stop_archive_pending` 为真，`stopping` 假 → 真 |
| 165 | 同上，finally | L | 自己在 146 设的 `stopping` | `stopping` → 假 |
| 236–241 | `persist_stopped_archive`，没有配置 final archive 的分支 | L | 调用方的 `starting` 或 `stopping` | 全部屏障清零，`stop_archive_members` 置 `None` |
| 243–244 | 同上，上传前 | L | 同上 | `stop_archive_pending` → 真，`members` 置为本次的成员快照 |
| 260–265 | 同上，上传并释放网桥之后 | L | 同上 | 全部屏障清零 |
| 286–288 | `retry_stopped_archive`，`prepare_final` 失败且 `persist=False` | L | 调用方的 `stopping` | `blocked` 和 `pending` → 真，`members` 保留 |
| 309 | 同上 | L | 同上 | 只改 `members`，`pending` 在 274 行读到为真 |
| 389 | `start_group` | L | 先在 387 等到没有启动在进行，再准入 `START` | `starting` 假 → 真 |
| 407 | `start_group.prepare_stopped_start` | L | `starting` | `start_archive_completion_pending` → 真 |
| 425 | `start_group`，完成上一次 completion 的重试 | L | `starting` | `start_archive_completion_pending` → 假 |
| 459–460 | `start_group`，start fence 已建立后的未知错误 | L | `starting` | `pending` → 启用 final archive 时为真，`members` 同步 |
| 478 | `start_group`，成功 | L | `starting` | `start_archive_completion_pending` → 假 |
| 484 | `start_group`，finally | L | `starting` | 除 `StaleStartFence` 外，`start_failed` ← 本次是否失败 |
| 485 | 同上 | L | `starting` | `starting` → 假 |
| 499 | `_begin_run_locked` | 调用方 L（405） | `starting` | `sealed_seq` ← max(`sealed_seq`, 旧 `start_seq`) |
| 502–503 | 同上 | 调用方 L | `starting` | `start_seq` ← 请求的 fence；`run_unfenced` ← fence 是否为空。fence ≤ `sealed_seq` 时抛 `StaleStartFence`，什么都不改 |
| 607–608 | `_stop_checked_group`，读 marker 失败 | L | `lifecycle_check` | `orphan_finalization_pending`、`orphan_marker_unreadable` → 真 |
| 628 | 同上，停止状态下分类 | L | `lifecycle_check` | `orphan_marker_unreadable` → 假 |
| 641 | 同上，从未运行过的 CREATED 容器 | L | `lifecycle_check` | `orphan_finalization_pending` → 假 |
| 654–656 | 同上，没有 marker 也没有策略 | L | `lifecycle_check` | `blocked`、`pending` → 真，`members` 置为成员快照 |
| 659–660 | 同上，需要重试但还没有 pending | L | `lifecycle_check` | `pending` ← 是否启用 final archive，`members` 同步 |
| 665 | 同上，隔离或 blocked | L | `lifecycle_check` | `stopping` → 真 |
| 675 | 同上，重试 | L | `lifecycle_check` | `stopping` → 真 |
| 678–679 | 同上，正常停止 | L | `lifecycle_check` | `stopping` → 真，`lifecycle_generation` 加一，令牌和成员进程清空 |
| 725、734、746 | 同上，三个分支的 finally | L | `lifecycle_check` | `stopping` → 假 |
| 781–783 | 同上，native flush 失败 | L | `lifecycle_check` + `stopping` | `blocked` → 真；`pending` 和 `members` 只在启用 final archive 时设置 |
| 800–801 | 同上，`prepare_final` 之前 | L | 同上 | `pending` → 真，`members` 置为本次快照 |
| 812–814 | 同上，网络失守时 `prepare_final` 失败 | L | 同上 | `pending`、`blocked` → 真 |
| 871 | 同上，except | L | 同上 | `stopping` → 假 |
| 883 | 同上，成功 | L | 同上 | `stopping` → 假 |
| 914–916 | `begin_archive` | L | `lifecycle_check`；准入 `ARCHIVE`，或同一请求重入时 `DELETION` | 首次时 `lifecycle_generation` 加一；`archive_request_id` ← 请求；`stopping` → 真。检查在 926 行结束，`stopping` 一直保留 |
| 940–942 | `abort_archive` | L | 准入 `LIFECYCLE_CHECK`；`archive_request_id` 必须是自己 | `archive_request_id` → `None`，`stopping` → 假，`lifecycle_generation` 加一 |
| 978–979 | `commit_archive` | L | `lifecycle_check`；准入 `IDLE_DELETION` 或 `DELETION`（`allow_lifecycle_check`） | `archive_request_id` ← 请求，`stopping` → 真；随后 group 被移除（1006） |

### 3.2 `group_reconciliation.py`

`reconcile`（77 行起）只为一个 group 开检查（162），条件是 102–104 行确认它没有 `lifecycle_check`、没有 `starting`、没有 `stopping`。下面这些都在 `reconcile_checked_group` 里，都在 L 内，持有者都是这次 reconcile 的 `lifecycle_check`。

| 行 | 分支 | 前 → 后 |
|---|---|---|
| 112 | 新发现的容器（构造） | `sealed_seq` ← 这个 group 最后一次 checkpoint 的 seq |
| 226–227 | 新发现，marker 读不出来 | `orphan_*` 两个 → 真 |
| 253–256 | 新发现，history 未知 | `pending`、`blocked` → 真，`members` 置为快照；`stopping` ← 容器是否在运行（无准入） |
| 269–270 | 新发现，有 marker | `orphan_finalization_pending` → 真，`stopping` → 真（无准入；group 是刚建的） |
| 290 | `orphan_marker_unreadable` 分支，读成功 | `orphan_marker_unreadable` → 假 |
| 306–310 | 同上，history 未知 | `pending`、`blocked` → 真，`members` 置为快照，`orphan_finalization_pending` → 假，`stopping` ← 是否运行（无准入） |
| 315 | 同上，marker 存在 | `stopping` → 真（无准入） |
| 319 | 同上，marker 不存在 | `orphan_finalization_pending` → 假 |
| 321 | `stop_archive_blocked` 分支 | `stopping` ← 是否运行（无准入） |
| 325 | `orphan_finalization_pending` 分支 | 准入 `ORPHAN_FINALIZATION` 后，`stopping` → 真 |
| 330 | `stop_archive_pending` 分支 | 准入 `TAKE_STOP` 后，`stopping` → 真 |
| 345–346 | 停止且网桥仍在，marker 读不出来 | `orphan_*` 两个 → 真 |
| 362–364 | 同上，没有 marker，启用 final archive | `pending`、`blocked` → 真，`members` 置为快照 |
| 368 | 同上，有 marker | 准入 `TAKE_STOP` 后，`stopping` → 真 |
| 422 | blocked 分支的 finally | `stopping` → 假 |
| 444 | finalize 成功 | `orphan_finalization_pending` → 假 |
| 447–450 | `SandboxArchiveBlocked` | `pending`、`blocked` → 真，`members` 置为快照，`orphan_finalization_pending` → 假 |
| 476–477 | 其他异常 | `orphan_finalization_pending` → 真；`orphan_marker_unreadable` ← 是否启用 final archive |
| 508 | retry/finalize 的 finally | `stopping` → 假 |

### 3.3 其余文件

| 位置 | 方法 | 锁 | 持有者 | 前 → 后 |
|---|---|---|---|---|
| `shared_session_state.py:342–343` | `mark_container_lost_locked` | 调用方 L | 任意 | 第一次时 `lifecycle_generation` 加一；`container_lost` → 真，从此不再清除 |
| `shared_session_state.py:368–369` | `accept_inspection_locked` | 调用方 L | 任意调用方 | `initial_archive_preservation_pending` 为真且容器已停止时，它和 `snapshotting` 一起 → 假，**不管 `snapshotting` 当前归谁** |
| `shared_session_state.py:376、384` | `begin_lifecycle_check_locked` | 调用方 L | 准入 `LIFECYCLE_CHECK`（只拒已有检查） | `lifecycle_generation` 加一，`lifecycle_check` ← 新检查 |
| `shared_session_state.py:408–409` | `end_lifecycle_check_locked` | 调用方 L | 只有这个检查本身 | `lifecycle_check` → `None`，`lifecycle_generation` 加一 |
| `shared_session_state.py:465、467` | `join_run_locked` | 调用方 L | 准入 `JOIN_RUN` | fence 为空时 `run_unfenced` → 真；否则 `start_seq` ← max。**不检查 `sealed_seq`** |
| `group_registration.py:155` | `register_manifest`（构造） | L | — | `sealed_seq` ← checkpoint seq |
| `group_environment.py:107` | `prepare_environment` | L | 准入 `MUTATION`（拒 `SNAPSHOTTING`） | `snapshotting` 假 → 真 |
| `group_environment.py:160` | 同上，finally | L | 自己的 `snapshotting` | `snapshotting` → 假 |
| `initial_archive_capture.py:82、86` | `_require_initial_archive_admission(own_barrier=True)` | 调用方 L | 自己的 `snapshotting` | 为了复用 `MUTATION` 的检查，先清掉再恢复（同一次持锁内，别人看不到） |
| `initial_archive_capture.py:154` | `capture_initial_archive` | L | 准入 `MUTATION` | `snapshotting` 假 → 真 |
| `initial_archive_capture.py:184、208` | 同上，两个 except | L | 自己的 `snapshotting` | `snapshotting` → 假 |
| `initial_archive_capture.py:251` | 同上，冻结后捕获失败、marker 可能已写 | L | 自己的 `snapshotting` | `initial_archive_preservation_pending` → 真 |
| `initial_archive_capture.py:328–329` | 同上，可以释放 | L | 同上 | `preservation` 和 `snapshotting` → 假 |
| `initial_archive_capture.py:332` | 同上，仍需保留 | L | 同上 | `preservation` → 真，`snapshotting` 不动 |

## 4. 哪些组合可达

记号：S = `starting`，T = `stopping`，N = `snapshotting`，C = `lifecycle_check`。

### 4.1 每个槽至多一个持有者

| 槽 | 结论 | 依据 |
|---|---|---|
| S | **已证明** | 唯一的置真点是 389。它前面在同一次持锁里先 `wait_for_start_locked`（387，等到 `START_WAIT` 不再被 S 拒绝），再准入 `START`。超时会抛 `SessionBusy`，不会置真 |
| C | **已证明** | 唯一的设置点是 384，前面先准入 `LIFECYCLE_CHECK`（375），已有检查时拒绝。408 只清除检查自己 |
| N | **有条件成立** | 两个置真点（`group_environment.py:107`、`initial_archive_capture.py:154`）都先准入 `MUTATION`，而 `MUTATION` 会拒 `SNAPSHOTTING`。例外是 `accept_inspection_locked`（369）：它会替别人清掉 N。只有当 N 属于持有 preservation 的初始 archive 时，这才是对的（见 §4.4 U2） |
| T | **在关机顺序成立的前提下成立** | 见下文 |

T 的置真点和它们各自凭什么排他：

- `_stop_checked_group`（665、675、678）：先准入 `STOP`（拒 T 和 S），再开 C。别的 T 持有者要么也要开 C（会被拒），要么检查 T（会被拒）。
- reconcile（256、270、310、315、321、325、330、368）：102–104 行确认没有 T，然后在同一次持锁里开 C。之后要设置 T 的路径都需要 C，或者会检查 T。只有一个例外，见下面关机重试那一条。
- `begin_archive`（916）：准入 `ARCHIVE`，它包含 `MUTATION`，而 `MUTATION` 会拒 T；同一请求重入时走 `DELETION`，这时 T 本来就是它自己的。
- `commit_archive`（979）：在 971 行准入 `IDLE_DELETION`（同样包含 `MUTATION`），或者同一请求重入时准入 `DELETION`。`container_lost` 时，`DELETION` 第一行就抛 `RetainedChatLost`，不会走到 979。
- `stop_all_for_shutdown`（146）：只准入 `TAKE_STOP`，它**不拒 S，也不拒 C**。所以只看准入表，它可以在一次正在失败的启动里（启动在 459 或 243 设了 `stop_archive_pending`）拿到 T；也可以在 reconcile 的 310、321 这两个不做准入的分支之前拿到 T。排除这两种情况的，是 `sandbox_manager.py` 的关机顺序：279 行 `endpoint_calls.seal_and_wait()` 先排空所有已准入的请求，所以没有启动还在进行；293 行 `quiesce` 再 join 后台扫描线程，并排空 recovery 的执行器（`shared_session_recovery.py:49`），所以也没有 reconcile。之后 296 行才调用 `stop_all_for_shutdown`。

**结论：T 至多一个持有者，但证明依赖另一个模块里的调用顺序，不是准入表本身能保证的。** §6 第 4 步把这条依赖收进准入表。

另外有一条已证明的蕴含：除 `ARCHIVE_HOLD` 和关机重试外，T 的持有者同时持有 C，并且在结束 C 之前清掉 T。`stop_if_idle` 在 570 行结束检查，而 `_stop_checked_group` 的每一条返回路径都先清了 T（725、734、746、871、883，或者在 667–673 行置 T 之前就返回）。reconcile 也一样（422、508），只有 marker 读不出来时的提前返回例外，而那条路径不设置 T。

`archive_request_id` 非空蕴含 T。唯一清除 `archive_request_id` 的地方（940）同时清 T；其他清 T 的路径都先准入了一个会拒 T 的操作，所以 `ARCHIVE_HOLD` 期间不会被别人清掉。**已证明**（同样以关机顺序为前提）。

### 4.2 进行中的操作两两组合

| 组合 | 可达吗 | 路径 |
|---|---|---|
| S + N | **可达，按设计** | 工作区引用在 82 行准入 `MUTATION`，然后释放锁去调 `admit_running`；这时初始 archive 拿到 N（`initial_archive_capture.py:154`）；接着引用的 `start_group` 拿到 S，因为 `START` 不看 N。初始 archive 157–159 行的注释写明了这种情况，并在 160–170 行等它结束。环境准备期间成员激活的启动也一样（`member_sessions.py:191` 等的是 `ACTIVATION_WAIT`，之后 228 行在锁外启动） |
| N + T | **可达，按设计** | 初始 archive 冻结后捕获失败，自己调 `stop_if_idle(reason="orphaned_attempt")`（`initial_archive_capture.py:253`）；`STOP` 不看 N。网络失守的 fail-stop（`guard_network`，`reason="unguarded"`）也一样 |
| N + C | **可达** | 同上：停止要开 C，而 C 不看 N |
| T + C | **可达** | 普通停止和 reconcile 在 C 下设置 T；`ARCHIVE_HOLD` 期间 `commit_archive` 再开 C（959） |
| S + C | **只在 `container_lost` 时可达** | `commit_archive` 在 `archive_request_id` 为空且 `container_lost` 时，不做准入就开 C（955–959）。`container_lost` 可以在启动进行中被别的线程置真，例如 `status` 发现容器 id 变了。随后 971 行的 `DELETION` 会因为 `CONTAINER_LOST` 抛 `RetainedChatLost`。其他开 C 的路径都会拒 S |
| S + T | **正常运行不可达** | 只有 §4.1 说的关机重试能走到，而关机顺序排除了它。没有任何其他路径 |

三个及以上同时成立：N + T + C 可达（同 N + T 那条路径）。S + N + C 只在 `container_lost` 时可达。其余含 S + T 的组合都不可达。

### 4.3 记录

- **`start_failed`。** 默认为假，只在 484 行写。每次启动结束时（`StaleStartFence` 除外）重写为"这次是否失败"。`start_group` 总是带 `retry_failed=True` 进入（387），所以 **S + `start_failed` 在每次重试时都成立**（`workspace_references.py:78` 也带 `retry_failed=True`）。它不随停止清除：一个启动失败、随后被停掉的 group，直到下一次启动成功前都保持 `start_failed`。读它的地方有：`wait_for_start_locked` 的非重试调用方（`member_sessions.py:301、335、360`，`workspace_references.py:86`）、`shared_admission_proof.py:60、85`（不复用旧的网络证明）、`environment_ready_boot.py:78`。
- **`container_lost`。** 只置真，从不清除（343）。一个 lost 的 group 不会复活，只会被 `commit_archive` 移除，或者被新的注册替换。它可以和 S、T、N、C 中的任何一个同时成立，因为它可能在任何 I/O 期间被置真。它成立以后，新的启动被拒（`shared_session_state.py:444`），`stop_if_idle` 被拒（537），删除只剩 `RetainedChatLost`（`DELETION` 第一行）。`MUTATION` 不看它，但 `MUTATION` 的调用方随后都会调 `accept_inspection_locked`，而它会对 lost 的 group 抛错（364）。

### 4.4 屏障

| 编号 | 不变量 | 结论 | 依据 |
|---|---|---|---|
| I1 | 启用 final archive 时，`stop_archive_blocked` ⇒ `stop_archive_pending` | **已证明** | 每个把 blocked 置真的地方都同时把 pending 置真（286–287、654–655、812–813，reconcile 253–254、306–307、362–363、447–448），只有 782 行例外：它的 pending 取 `_archives_finals`。清除时两者总在一起（236–237、260–261） |
| I1′ | 不启用 final archive 时，blocked 可以单独成立 | **可达，短暂** | 782 行。随后 `stop_blocked_group` 在 361 行不提前返回，367 行的 `persist_stopped_archive` 清掉它。只有中间抛错时才会留下来 |
| I2 | `stop_archive_pending` ⇔ `stop_archive_members is not None` | **已证明**，前提是 T 至多一个持有者，并且 S + T 不可达 | 所有赋值都成对（459–460、659–660、781/783 等），只有 309 行单独写 `members`，而它的 pending 在 274 行读到为真。在这两次持锁之间能清 pending 的只有另一个 `persist_stopped_archive`，它要求调用方同时持有 S 或 T |
| I3 | `orphan_marker_unreadable` ⇒ `orphan_finalization_pending` | **已证明**，前提是 C 和 T 各自至多一个持有者 | 置真总是成对（607–608、226–227、345–346、476–477）。单独清 pending 的地方（641、309、319、444、450）都在 unreadable 已经被清掉（628、290）之后，或者在 unreadable 不可能为真的分支里（reconcile 的分支顺序先处理 unreadable） |
| I4 | `start_archive_completion_pending` 只在 S 下被置真 | **已证明** | 唯一的置真点是 407 行。它在 S 结束后可以留下：启动的 `archive.started` 失败时，pending 留下，`start_failed` 被置真。这正是 `recover_start_archive_completion`（1042）要重试的状态 |
| U1 | 至多一个 T 持有者，并且 S + T 不可达 | **依赖关机顺序** | §4.1 |
| U2 | `accept_inspection_locked` 只清掉初始 archive 自己的 N | **未证明** | 369 行不看 N 属于谁。要证明，就得证明 preservation 为真时 N 一定属于那个初始 archive。I5 的反例路径说明这一点证明不了 |
| I5 | `initial_archive_preservation_pending` ⇒ N | **未证明，可能是竞态** | 候选反例：preservation 在 251 行置真；捕获在锁外调 `stop_if_idle`，停止中途，`accept_inspection_locked` 看到容器停了，于是把 preservation 和 N 一起清掉（369）；`stop_if_idle` 随后仍以失败返回，261–281 行等 T 结束再重新查看容器；这时如果另一个请求已经重新启动了这个 group（`START` 不看 preservation 和 N），容器就是在运行的，于是 `release_snapshot_barrier` 为假；332 行把 preservation 重新置真，但 N 已经是假的，而且也可能已经被别的 `MUTATION` 拿走 |
| U3 | 运行中 `start_seq` 为空或大于 `sealed_seq` | **未证明** | `_begin_run_locked` 拒绝 ≤ `sealed_seq` 的 fence（501），但 `join_run_locked` 不检查（466–467）。重启后发现的运行中 group，`sealed_seq` 取自 checkpoint（构造时），之后加入的请求的 fence 没有和它比较 |
| I6 | `sealed_seq` 只增不减 | **已证明** | 只在 499 行，以 max 的方式写 |
| I7 | `run_unfenced` 在一次运行内只会从假变真 | **已证明** | 465 行只置真；503 行只在新运行开始时重写 |

### 4.5 A1 承诺的组合计数日志

结构重构 §5.1 说 A1 要"按组合记一条计数日志，作为 A2 的佐证"。基线上没有这条日志：`group_lifecycle.py` 里没有，`grep` 也找不到。所以 §6 第 0 步先补上它。

## 5. 新表示

### 5.1 数据

放在 `group_lifecycle.py`。`_Group` 只保留一个字段 `life: GroupLifecycleState`。准入表 `ADMISSION` 和 `Blocker` 不变，只把每个 `Blocker` 的判断（`_HOLDS`）改成读新字段。

```python
class StopHolder(Enum):
    STOP = "stop"                    # stop_if_idle，在自己的 lifecycle check 下
    FINALIZE = "finalize"            # reconcile 的 retry、finalize、blocked 停止，在自己的 check 下
    SHUTDOWN_RETRY = "shutdown"      # stop_all_for_shutdown 的 archive 重试
    ARCHIVE_HOLD = "archive_hold"    # 管理员 archive 请求，直到 commit 或 abort

class ExclusiveHolder(Enum):
    ENVIRONMENT = "environment"          # prepare_environment
    INITIAL_ARCHIVE = "initial_archive"  # capture_initial_archive

@dataclass(frozen=True, eq=False)
class Hold:
    """一次进行中的操作；只有拿到它的调用方能结束它。"""
    kind: StopHolder | ExclusiveHolder | Literal["start"]
    request_id: str | None = None        # 只给 ARCHIVE_HOLD

@dataclass(frozen=True)
class FinalArchive:
    """停止后欠下的 final archive。

    members 非空就是旧的 stop_archive_pending（I2）；
    blocked 就是旧的 stop_archive_blocked。
    启用 final archive 时，blocked ⇒ members 非空（I1）。
    """
    members: tuple[SharedMemberManifest, ...] | None
    blocked: bool = False

@dataclass(frozen=True)
class OrphanFinalization:
    marker_unreadable: bool = False      # I3：unreadable 不能单独存在

@dataclass(frozen=True)
class RunLabel:
    start_seq: int | None = None
    unfenced: bool = False
    sealed_seq: int | None = None

@dataclass
class GroupLifecycleState:
    start: Hold | None = None
    stop: Hold | None = None
    exclusive: Hold | None = None
    check: _LifecycleCheck | None = None
    initial_preservation: bool = False   # 见 U2、I5：暂不并入 exclusive
    last_start_failed: bool = False
    container_lost: bool = False
    final_archive: FinalArchive | None = None
    orphan: OrphanFinalization | None = None
    start_completion_pending: bool = False
    archive_request_id: str | None = None  # 等价于 stop.kind is ARCHIVE_HOLD 时的 request_id
    run: RunLabel = RunLabel()
    generation: int = 0
```

- **四个槽而不是一个集合。** 每个槽已经证明至多一个持有者（§4.1），而且只有 S + T 一对不可达（§4.2）。用集合表示不出"同一种操作不能有两个"，用一个 Optional 又表示不出按设计就会同时成立的 S + N、N + T。
- **槽里记录持有者。** 旧代码里，"谁设置的 `stopping` 就由谁清除"只靠调用方自觉。新表示里，结束操作必须交回 `begin` 时拿到的那个 `Hold`；交回的不是当前持有的那个就抛 `AssertionError`。这样 U2 那种"替别人清除"就会立刻暴露出来。
- **`archive_request_id` 暂时保留独立字段**，因为 `begin_archive` 和 `commit_archive` 都按它做幂等判断（902、955）。不变量是 `archive_request_id is not None` ⇔ `stop.kind is ARCHIVE_HOLD`（§4.1，已证明）。
- **`initial_preservation` 不并入 `exclusive`**，因为 I5 没有证明。如果 §7 Q2 决定 `accept_inspection_locked` 只清 `INITIAL_ARCHIVE` 自己的 N，就把它改成 `exclusive` 上的一个属性。
- **线上和日志的名字不变。** `SharedSessionDescriptor.snapshotting`、`lifecycle_pending` 照旧由新字段推出：`snapshotting = exclusive is not None`，`lifecycle_pending = check or stop or final_archive.members or orphan`。`sandbox_deletion_diagnostics.py:92` 读的字段名改成从新表示推出的同名值。

### 5.2 转移 API

所有方法都以 `_locked` 结尾，要求调用方持有 `state.lock`。调试构建里用 `RLock._is_owned()` 断言这一点。每个方法只做"检查前置条件、写字段、需要时 `notify_all`"三件事；准入仍然调 `admit`。

| 方法 | 前置条件（不满足就 `AssertionError`） | 效果 | 替换的赋值 |
|---|---|---|---|
| `begin_start_locked() -> Hold` | `start is None`；调用方已等到 `START_WAIT` 放行并准入 `START` | `start = Hold("start")` | 389 |
| `end_start_locked(hold, outcome)` | `start is hold` | `outcome` 取 `COMPLETED`、`FAILED`、`REFUSED`；后两种之外都重写 `last_start_failed`；`start = None`；notify | 484–485 |
| `begin_run_locked(start_seq)` | `start is not None` | 同现在的 `_begin_run_locked`，包括 `StaleStartFence` | 499–503 |
| `join_run_locked(start_seq)` | 准入 `JOIN_RUN` | 同现在（U3 未决，见 Q4） | 465–467 |
| `owe_start_completion_locked()` / `settle_start_completion_locked()` | 前者要求 `start is not None` | 置真 / 置假 | 407 / 425、478 |
| `begin_stop_locked(kind, *, request_id=None) -> Hold` | `stop is None`；`kind` 是 `STOP` 或 `FINALIZE` 时，`check` 必须属于同一调用方 | `stop = Hold(kind)`；`ARCHIVE_HOLD` 同时写 `archive_request_id` | 146、665、675、678、916、979，reconcile 256–368 |
| `end_stop_locked(hold)` | `stop is hold` | `stop = None`；`ARCHIVE_HOLD` 同时清 `archive_request_id`；notify | 165、725、734、746、871、883、941，reconcile 422、508 |
| `begin_exclusive_locked(kind) -> Hold` | `exclusive is None` | 设置 | `group_environment.py:107`、`initial_archive_capture.py:154` |
| `end_exclusive_locked(hold)` | `exclusive is hold` | 清除；notify | `group_environment.py:160`、`initial_archive_capture.py:184、208、329` |
| `admit_as_locked(hold, operation)` | `exclusive is hold` | 准入时不把这个 hold 自己算作 `SNAPSHOTTING` | `initial_archive_capture.py:82、86` 的先清后恢复 |
| `preserve_initial_locked(hold)` / `release_initial_locked()` | 前者要求 `exclusive is hold` | 置真 / 置假 | 251、332 / 328 |
| `begin_check_locked()` / `end_check_locked(check)` | 同现在 | 同现在 | 376–384 / 408–409 |
| `mark_lost_locked()` | — | 同现在 | 342–343 |
| `owe_final_locked(members, *, blocked=False)` | `stop` 或 `start` 不为空 | `final_archive = FinalArchive(members, blocked or 现有 blocked)`；不启用 final archive 时，`members` 传 `None` | 243–244、286–288、309、459–460、654–660、781–783、800–801、812–814，reconcile 253–255、306–308、362–364、447–449 |
| `block_final_locked()` | 同上 | 只置 `blocked` | 782（不启用 final archive 时） |
| `owe_orphan_locked(*, marker_unreadable)` | `check` 或 `stop` 不为空 | 设置 `orphan` | 607–608、226–227、269、345–346、476–477 |
| `marker_read_locked()` | 同上 | `orphan.marker_unreadable = False`；`orphan` 保留 | 628、290 |
| `settle_orphan_locked()` | 同上 | `orphan = None` | 641、309、319、444、450 |
| `settle_archive_locked()` | 同上 | `final_archive`、`orphan` 清空，`start_completion_pending` 置假 | 236–241、260–265 |
| `accepted_stopped_locked()` | — | 由 `accept_inspection_locked` 调用；只有在 `exclusive.kind is INITIAL_ARCHIVE` 且 `initial_preservation` 时才清这两项（Q2 决定前，为保持行为不变，先照旧不看持有者，只记一条 `group_lifecycle_foreign_exclusive_release` 日志） | 368–369 |

`stop_archive_members` 单独写（309）的场景变成 `owe_final_locked(members)`，并断言 `final_archive.members is not None`。这条断言就是 I2。

### 5.3 不变量写成代码

`GroupLifecycleState.check_invariants(archives_finals: bool) -> list[str]` 返回违反了哪些不变量。调试构建和测试在每次转移后调用它；生产构建只在 §6 第 0 步的计数日志里记下违反情况，不抛错。

- 已证明的：I1（启用 final archive 时）、I2、I3、I6、I7、`archive_request_id` ⇔ `ARCHIVE_HOLD`、`start_completion_pending` 只在 `start` 下才能从假变真。
- 依赖关机顺序的：U1，即 `not (start and stop)`。第 4 步之后，它成为已证明的不变量。
- 未证明的：I5 和 U3 只记日志，不断言。

## 6. 迁移步骤

每一步一个 PR。第 0–3 步行为不变：测试只改 import 和 patch 路径，不改断言（AGENTS.md §5 第 3 条）。

0. **组合计数。** 在 `_HOLDS` 旁边加 `GroupLifecycle.combination()`，返回 (S, T 及其持有者, N 及其持有者, C, `start_failed`, `container_lost`, 每个屏障) 的元组。在 `begin_lifecycle_check_locked`、`end_lifecycle_check_locked`、`mark_container_lost_locked`、`start_group` 的 finally、`_stop_checked_group` 的每个 finally 里调用一次。每个进程第一次见到某个组合时记一条 `group_lifecycle_combination` 日志，再按小时汇总计数。staging 跑一周，结果拿来核对 §4，只作佐证，不作依据。
1. **新字段加转移 API，旧字段名变成只读属性。** 引入 `GroupLifecycleState` 和 §5.2 的方法，把 §3 的 122 处赋值全部改成调用方法。`_Group.starting` 等改成只读 `@property`，读取方暂时不动。同时 `_HOLDS` 改为读新字段。这一步只改写入方，diff 机械，便于审阅。
2. **读取方迁移。** 读取方改成查 `group.life` 或 `group.lifecycle.holds(...)`，然后删掉只读属性。`initial_archive_capture.py:134–143、160–170` 那两段手写的等待改成 `busy_reason` 的两张新表行（`INITIAL_ARCHIVE_WAIT`、`INITIAL_ARCHIVE_DRAIN`），这样等待的理由也来自准入表。
3. **持有者断言。** 打开 §5.2 的 `Hold` 校验和 §5.3 的不变量检查（测试和调试构建）。`accept_inspection_locked` 那一处按 §5.2 的过渡写法保留原行为。
4. **收紧关机重试的准入（行为改动，单独 PR）。** `TAKE_STOP` 拆成两行：reconcile 用的 `TAKE_STOP` 保持原样，因为它已经持有 C；`stop_all_for_shutdown` 用新的 `SHUTDOWN_TAKE_STOP`，额外拒绝 S 和 C。按 §4.1，在关机顺序不变时它不会改变任何结果；它的作用是让 U1 不再依赖 `sandbox_manager.py` 的调用顺序。被拒时照旧记进 `failures`。
5. **棘轮测试。** `tests/unit/test_group_lifecycle_writes.py` 用 §3 的 AST 扫描，断言 `cloud_agent_host/` 里除 `group_lifecycle.py` 外，没有任何语句给 `GroupLifecycleState` 的字段或旧字段名赋值，也没有 `setattr`。同时把结构重构 §14 表里"supervisor 里的 `SessionBusy(`"那一行换成"生命周期字段只在 `group_lifecycle.py` 里赋值"。

Q1–Q4 的决定可以插在第 3 步之后任何时候做，每个单独一个 PR。

## 7. 测试

### 7.1 表驱动

`tests/unit/test_group_lifecycle.py` 已经对 `ADMISSION` 按行测试（307 行起）。新增两张表：

- **转移表。** 每行是"起始状态、调用、期望的状态或异常"。§5.2 的每个方法至少有三类行：前置条件满足、前置条件不满足（`AssertionError`）、在会改变结果的记录或屏障下调用（例如 `container_lost` 时 `begin_start_locked`；`archives_finals=False` 时 `owe_final_locked`）。
- **准入对照表。** 对每个 `Operation`，用旧的 flag 构造 `_Group`（基线代码里的 `_group(**fields)` 助手，42 行），再用 §5.1 构造等价的新状态，断言两边 `refusal()` 给出同一个异常类型和消息。这张表在第 1 步落地，第 2 步删掉旧字段时，旧那一侧改成固定的期望值。

### 7.2 穷举

状态空间是有限的，直接全部枚举：

- `start` ∈ {无, 有}；`stop` ∈ {无, STOP, FINALIZE, SHUTDOWN_RETRY, ARCHIVE_HOLD}；`exclusive` ∈ {无, ENVIRONMENT, INITIAL_ARCHIVE}；`check` ∈ {无, 有}；
- `last_start_failed`、`container_lost`、`initial_preservation`、`start_completion_pending` 各取真假；
- `final_archive` ∈ {无, 待上传, 待上传且 blocked, 只有 blocked}；`orphan` ∈ {无, 待处理, 待处理且 marker 读不出来}；
- 成员占用者 ∈ {无, 有}；`inspection.running` ∈ {真, 假}；`archives_finals` ∈ {真, 假}。

共 2×5×3×2×2⁴×4×3×2×2×2 = 92,160 个状态。每个状态只算一次 `refusal`，一秒以内跑完。三项检查：

1. **准入一致。** 对每个状态和每个 `Operation`，新实现的结果等于第 1 步之前旧实现在等价 flag 上的结果。这一步需要把旧的 `_HOLDS` 复制一份放在测试里，作为固定的参照。
2. **不变量封闭。** 从满足 §5.3 已证明不变量的每个状态出发，调用每个转移：要么前置条件拒绝，要么结果仍然满足这些不变量。
3. **可达集合。** 从初始状态出发，按转移 API 做广度优先搜索，同时遵守准入表：只有 `admit` 放行时才调用对应的 `begin_*`。断言得到的进行中操作组合集合正好等于 §4.2 的表：S + N、N + T、N + C、T + C、N + T + C 可达；S + C 只在 `container_lost` 时可达；第 4 步之后 S + T 不可达。以后有人加了新转移、产生了新组合，这个测试就会失败，直到同时更新本文和这张表。

### 7.3 不变的部分

- `tests/unit/test_shared_session_supervisor_lifecycle.py`（1,770 行）和其他并发测试的断言一个都不改。
- 第 4 步加一个用例：关机时如果有启动还在进行，`stop_all_for_shutdown` 会把这个 group 记进 `failures`，而不是拿到 `stopping`。
- 第 1–4 步各自在 staging 的 Kata host 上验收一遍，内容同结构重构 §14 对 host PR 的要求。重点是带 final archive 的停止、网络失守 fail-stop、初始 archive 失败后的保留，以及管理员删除。

## 8. 待决问题

- **Q1：U1 靠关机顺序，还是靠准入表？** 本文建议后者（第 4 步）。代价是一条 `TAKE_STOP` 变成两条，好处是不变量从此是局部的。
- **Q2：`accept_inspection_locked` 该不该替别人清 N？** 369 行在容器停止时清 preservation 和 N，不看 N 是不是初始 archive 自己的。建议：只有 `exclusive.kind is INITIAL_ARCHIVE` 时才清。这是行为改动，要先确认 I5 的反例路径在实际中能不能走到，确认的办法是看第 0 步的日志里有没有 `preservation ∧ ¬N` 这个组合。
- **Q3：I5 的反例算不算 bug？** 如果能走到，332 行重新置真的 preservation 没有 N 来配合，新的写入者就可能在 preservation 期间被准入（`MUTATION` 不看 preservation）。建议在 332 行要求 N 仍然属于自己；如果不属于，就把这个 group 记为 `initial_archive_preservation_lost` 并停掉它。需要写[推迟初始 archive 上传](2026-11-03-deferred-initial-archive-upload.md)的人确认。
- **Q4：`join_run_locked` 要不要检查 `sealed_seq`？** 重启后发现的运行中 group，`sealed_seq` 来自 checkpoint，而加入它的请求的 fence 没有和这个值比较（U3）。要确认控制面的 start fence 能不能出现"比 checkpoint 旧、却在 checkpoint 之后才到达"的请求。如果不能，就加断言；如果能，就加拒绝（`StaleStartFence`）。
- **Q5：`start_failed` 该不该随停止清除？** 现在一个启动失败后又被停掉的 group 会一直带着 `start_failed`，直到下一次启动成功，这期间 `environment_ready_boot.py:78` 和 `shared_admission_proof.py:60` 都会因此拒绝。这可能是有意的（"上一次启动失败"本来就是要求重新证明网络的理由），但没有任何地方写下这个决定。
- **Q6：`MUTATION` 要不要直接拒 `CONTAINER_LOST`？** 现在靠调用方随后调 `accept_inspection_locked` 抛错。加一行 `Refusal` 能让准入表自己说清楚，但错误类型会从 `SessionBusy` 之后的 `RetainedChatLost`，变成准入时直接抛 `RetainedChatLost`，要核对调用方的处理是否一致。
- **Q7：锁外的准入。** `shared_admission_proof.py:100–101` 的 `ensure_running` 在不持锁的情况下调 `admit(Operation.RUN)`，并写 `group.inspection`。这不属于本文的字段，但 A2 第 1 步加了持锁断言之后，这里会立刻失败，所以要在第 1 步之前先修：把这两行放进 `with lifecycle.state.lock`。
