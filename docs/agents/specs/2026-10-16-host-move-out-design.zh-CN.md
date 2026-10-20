# 计划内腾空：租期将尽的 Host 把会话逐个搬走

**状态：** 已实现。依赖 [跨机器恢复](2026-10-13-sandbox-cross-host-restore-design.zh-CN.md)（#658）。

## 1 要解决的问题

CloudLab 机器有租期，续期常被拒绝。到期时机器被收回，机器上的会话也一起没了。

#658 让一台**被移除**的 Host 上**已停止、且有检查点覆盖**的 group 变成 `cold`，下一轮由别的 Host 从检查点恢复。但到期前后还有三个缺口：

1. 到期前 30 分钟到 5 分钟之间，老会话的后续轮次仍在这台 Host 上运行。group 要闲置满 20 分钟才停，所以到期时很多 group 还开着。撤销凭证时它们不满足"已停止、有检查点"，搬不走。
2. 自动扩缩容池把快到期的成员仍算作可用容量，等它真没了才补新的。
3. 手动添加的服务器到期后，provider 确认机器已终止，但它的 Host 一直不撤销，group 一直绑在一台已经不存在的机器上（2026-10-15 staging 上的 ms1125 就是这样）。

## 2 做法

到期前一段时间（默认 2 小时），Host 进入**腾空**：

- 这台 Host 不再领取任何任务；
- 正在运行的一轮不打断，跑完为止；
- 每个闲置的 group 立即停下，停止时照常捕获检查点；
- 检查点一到，这个 group 单独转成 `cold`，不必等整台 Host 撤销；
- 这些会话的下一轮由别的 Host 领取，按 #658 从检查点恢复。

用户感知到的只是这条消息多等了几秒到一分钟。

整机轮换（#378）不变：开启 `rotation_enabled` 的分配仍整机迁移，不进入腾空。

## 3 设计

### 3.1 什么时候开始

`RenewalPolicy` 新增 `move_out_before_seconds`，默认 7200，0 表示关闭。生效窗口由 `move_out_window_seconds` 计算：

- `rotation_enabled` 时为 0；
- 开启续期时，窗口不超过 `extend_to_horizon_seconds - 600 - 900`，保证续期成功后剩余时间一定在窗口和下一次续期之外，不会刚续完又开始腾空。

续期在窗口打开前尝试：调度器的续期触发时间取 `renew_before_seconds` 与“窗口 + 900 秒”的较大者（`MOVE_OUT_RENEWAL_LEAD_SECONDS`）。提前的 15 分钟长于既未生效也未被拒绝的续期的等待时间（10 分钟，之后记为 `extension_unconfirmed`），所以窗口打开时续期已经有了结果：续期成功，到期时间后移，腾空不会开始，Host 也不会提前停止领取任务，自动扩缩容池也不会为它补机；续期失败，窗口才打开。如果续期在窗口打开后才成功（例如调度器停过），已经开始的腾空会自动结束。

### 3.2 腾空标记：`move_out` admission hold

迁移 `0083_host_move_out` 给 `agent_host_admission_holds.purpose` 增加 `move_out`。

每次 inspection，reconciler 按观测到的到期时间设置或清除这条 hold（`scope='all_claims'`，`source_id` 为 allocation ID）。`all_claims` 沿用现有语义：Host 不能领取任何任务，已运行的 attempt 照常结束。

**只有真能搬走时才设置。** reconciler 只告诉仓库"窗口到了没有"（`due`），设不设 hold 由 `set_move_out_hold` 决定，条件是：

1. **检查点能产生：** 归档开启且 `checkpoints_enabled`，这台分配的每个 Host 都声明 `sandbox-checkpoints-v1`，并已应用当前归档版本。否则停下的 group 没有检查点，会话只会一直排队。这一条每次都检查：检查点中途关闭，腾空也随之停止。
2. **有 Host 会接手：** 另有一台在线、已注册、声明恢复和归档能力、已应用当前归档版本的 Host，而且按 claim 自己的准入规则 `host_compute_admission` 现在既能领取、也能新建 group（恢复要新建容器，所以 `new_groups` 限制、租期窗口、观测过期的 Host 都不算）。要搬的 group 里有 `untrusted-kata-v1` 的，这台 Host 还必须运行 Kata：claim 只把这种会话交给 Kata Host，否则 group 转成 `cold` 后没有 Host 能恢复，会话一直排队（2026-10-17 修订）。否则单机部署会提前 2 小时卡住所有后续轮次。
3. **一旦开始就继续：** hold 已存在时，即使目标暂时消失也继续（第 1 条仍要满足）。

检查和写入在同一事务里，并取得与其他 hold 写入相同的排他拓扑锁。两台 Host 同时进入窗口、互为唯一目标时，先拿到锁的那台设上 hold；第二台再检查时，对方已在腾空、不能新建 group，不再算目标。所以不会两台都停止服务。

### 3.3 停下闲置的 group

后台任务 `coordinate_move_out`（每 15 秒）做两件事：

1. `release_moving_out_groups`：对所有腾空中的 Host 调用 `release_covered_sandboxes`，把已停止且检查点覆盖的 group 转成 `cold`。
2. `sandboxes_moving_out` + `request_move_out_stop`：对腾空 Host 上 `running` 的 group 写入与管理员停止相同的 `stop_requested_at` 标记。group 有运行中的一轮、操作或快照时返回 `busy`，下一轮再试；Host 侧在终端打开时也会拒绝。

停止由 Host 下一次心跳执行，最终归档产生检查点回执。

### 3.4 检查点到达即交出

`record_sandbox_checkpoint` 在把回执绑定为 `restore_checkpoint_id` 后，如果这台 Host 正在腾空，立即对这个 group 调用 `release_covered_sandboxes(..., sandbox_ids=[id])`。覆盖条件与 #658 第 6.2 节完全相同，不放宽。

**已领取、还没占用 group 的一轮不能被打断。** Host 在 hold 生效前可能已经领取了某个会话的下一轮。这一轮要等领取之后才 reserve group，此刻 membership 上还没有 `active_attempt_id`。如果这时把 group 交出去，reserve 会失败，合法的一轮会以 `lease_lost` 结束。因此腾空路径调用时传 `live_host=True`：会话有 `running` 的 job 时，group 不交出，等这一轮结束后由清扫再次停下。

两条腾空路径（清扫、检查点到达）都先取得与 hold 写入相同的拓扑锁，再锁 group 行，然后才执行交出：

- 拓扑锁保证读到的腾空列表在交出完成前不会被续期撤销；
- 领取会以共享锁持有 group 行，先锁 group 行，就能让交出语句看到所有已提交的领取。

撤销路径不传这个参数：被撤销 Host 上已领取的轮次会被 reaper 重新排队，group 仍照原规则交出。

### 3.5 旧 Host 不能领回自己的 `cold` group

容器名由 `(thread_id, sandbox_id)` 决定，不含 generation。仍在线的腾空 Host（或撤销后以同名重新注册的 Host）本地还留着旧容器，如果它领取了这个 `cold` group，恢复时会撞名并耗尽重试次数。

因此 claim 的 `cold` 分支要求：`s.container_id IS NULL OR s.host <> 领取方`。

- 刚转成 `cold` 的 group 仍记着旧容器，旧 Host 领不到；
- 一次恢复失败退回 `cold` 时，容器已清空（领取时置空），原恢复方可以重试。

这一条也修正了 #658 的一个隐患：撤销后同名重新注册的 Host 会领回自己的组。

### 3.6 Provider 确认终止

观测到 `terminated` 时，除了池成员原有的释放流程，所有分配都对其 Host 调用 `release_covered_sandboxes`。

这里**不撤销**凭证：Host 行、凭证和部署的 drain fence 留给管理员移除流程处理。机器已经不存在，group 不可能再在那里启动；3.5 保证旧 Host 不会领回。

### 3.7 自动扩缩容池

- 成员快照增加 `moving_out`（成员租期进入窗口）。腾空中的成员不计入 `min_hosts` 所需的容量，替换机在它仍在服务时就开始申请。
- 腾空成员仍计入 `max_hosts`。达到上限时最小容量补机静默等待，不在每轮记录拒绝。
- **建议：** `max_hosts` 至少比 `min_hosts` 大 1，并多预留一个部署配置，替换机才能与即将离开的成员重叠。
- 腾空完的成员仍等租期结束，由已有的终止释放流程收尾。提前释放需要放宽释放前的容器清点（旧 Host 上还留着已搬走 group 的停止容器），这一版不做。释放前清掉这些容器见第 8 节（2026-10-19）；池成员自己提前释放仍不做。

### 3.8 告警

到期前 `max(stop_claims_before_seconds, 300)` 秒，如果分配的 Host 上仍有未搬走的 group（不是 `cold`、`restoring`、`lost`、已删除），发送一次 `compute_move_out_unfinished`。可能的原因：一轮任务还没跑完、没有检查点，或者没有可去的 Host。

## 4 失败方向

| 情况 | 结果 |
|---|---|
| 没有其他可恢复的 Host | 不开始腾空，行为与以前相同 |
| 检查点上传失败或被跳过 | group 留在原 Host，告警；到期后与以前相同 |
| 很长的一轮跑过到期 | 不打断；到期后按现有规则处理，可能 `LOST`，告警 |
| 腾空期间续期成功 | hold 清除，未搬走的继续在原 Host；已 `cold` 的在别处恢复 |
| 恢复失败 | 按 #658：回到 `cold`，最多 3 次，之后 `lost` |

## 5 管理员移除

管理员在 Admin → Servers 点"移除"，原来的做法是：有运行中的任务就等它结束，没有就立即撤销凭证。闲着、但容器还开着的 group 不会先停下打包，撤销后只能留在原机器上。

现在"移除"默认也先腾空（迁移 `0084_host_removal_move_out` 新增 `agent_runner_hosts.removal_moves_out` 和 `removal_leaves_unmoved`）。

机器上每个还没交出去的 group 分成三类，判断条件写在共享 SQL 里，和真正交出时用的是同一个：

| 类别 | 条件 | 去向 |
|---|---|---|
| 已覆盖 | 已停止，且检查点覆盖它（`COVERED_GROUP_SQL`，就是 `release_covered_sandboxes` 的条件：同一容器、generation、启动序号，镜像可拉取，记录了每个成员最后结束的一轮，没有进行中的 fork 或任务） | 撤销时直接交出 |
| 还能搬 | 还没停（`provisioning`、`starting`、`running`、`stopping`），镜像可拉取，并且已经有容器，或者正有一轮在建它 | 停下时生成检查点，然后交出 |
| 搬不走 | 其余全部：已停止但没有覆盖它的检查点（比如当时上传失败）、脏停止、本地镜像、从没建出容器的 group（比如第一轮在 Docker 建好容器之前就失败了） | 留在原机器上 |

已经停下的 group 不会再补拍检查点，所以"搬不走"的 group 不会自己变好。从没建出容器的 group 也一样（2026-10-16 修订）：没有容器可停，就没有检查点；只有它自己的下一轮能建出容器，而腾空中的机器不再领取任务。原来把它算作"还能搬"，移除就一直等它，页面一直显示还剩 1 个。

1. **能搬就先搬。** 有"还能搬"的 group，并且 `move_out_possible` 成立（检查点能产生、有别的 Host 会接手，在同一把排他拓扑锁里判断），机器进入 `draining`，同时置 `removal_moves_out`，返回 `moving_out`。之后和到期腾空完全一样：不接任务，闲着的 group 停下、打包、交出去，正在跑的一轮不打断。机器上还有运行中的任务时，即使此刻没有要停的 group，只要 `move_out_possible` 成立也照样腾空（2026-10-16 修订）：正在恢复的 group，或者移除前已领取、移除后才启动的已覆盖 group，在这一轮结束后都在这台机器上运行，而撤销要等这一轮结束，那时它们已经交不出去了。
2. **会有 group 留下时先问管理员。** 有"还能搬"的 group 但没地方搬，返回 409 `no_move_target`；有"搬不走"的 group，返回 409 `cannot_move`。页面弹窗说明原因和"移除后这些会话留在原机器上，回来之前不能继续"，管理员确认后带 `allow_unmoved=true` 再发一次：能搬的照样先搬，搬完按原来的规则撤销。同意记在 `removal_leaves_unmoved`。只有"已覆盖"的 group 时不问，因为撤销时它们照样会被交出（#658）。
3. **搬完才撤销。** `finish_host_removals` 对带 `removal_moves_out` 的移除，除了等运行中的任务结束，还要等这台机器上没有"还能搬"的 group；有"搬不走"的 group 而管理员没同意时，也继续等。
4. **搬的过程中检查点失败。** 这个 group 从"还能搬"变成"搬不走"，移除停在那里，不会自己撤销，也不会一直卡着没有提示：主机行显示"N cannot move, remove again to leave them"。管理员再点一次移除，确认后带 `allow_unmoved=true`，移除继续；也可以点"恢复使用"取消移除，会话回到原机器上继续。
5. **页面显示进度。** 主机行显示"Moving out: N sandbox(es), M running turn(s) left"（来自 `sandboxes_left`，只数"已覆盖"和"还能搬"的），有搬不走的再加上数量（`sandboxes_unmovable`）。还有任务或 group 在搬时，管理员可以点"Remove now"，也就是原来的强制移除：运行中的任务立即结束，没搬走的 group 留在原机器上。
6. **"恢复使用"即取消。** undrain 清除 `removal_requested_at`，腾空随之停止。已经交出去的 group 留在新机器上。

已经在等待中的旧移除请求（`removal_moves_out=false`）保持原来的含义。

## 6 不在本版范围

- 腾空完的池成员提前释放（见 3.7）。释放前清掉旧容器见第 8 节。
- 失联时还在运行、或没有覆盖检查点的 group：它们的最新状态只在那台机器上（见附录）。

## 7 验证

- 单元：窗口计算、inspection 按窗口请求 hold、续期提前、告警只发一次、池容量计算、清扫任务。
- PostgreSQL：检查点未开启或原 Host 不捕获时不腾空、目标有 `new_groups` 限制时不算目标、两台同时进入窗口最多一台腾空、开始后目标消失仍继续；检查点到达即交出、清扫停止闲置 group 后交出、忙碌 group 不停、旧 Host 领不回自己的 group、provider 终止交出 group。
- 检查命令：`make check`、`make test-db`（独立测试库）、`make ci-database`、`make docs`。

## 8 释放前清掉搬走的 group 留下的容器（2026-10-19）

### 8.1 问题

group 交出去以后，它的停止容器还留在旧 Host 上：交出只改数据库，不碰 Docker。释放前的清点（Sandbox Manager 的清单和 SSH 全机扫描）只要看到一个聊天容器就拒绝释放。第三轮验收里，管理员移除机器、两个 group 交出后，主动释放一直停在 `checking_retention`，只能等租期到期。

不能等"新 Host 出现更高的 generation"再清：交出时 group 只变成 `cold`，generation 要等下一轮被领取才加 1。用户一直不再发消息，group 就一直 `cold`，旧容器会一直挡住释放。

### 8.2 什么样的容器可以删

释放时，控制面按 Sandbox Manager 报上来的每个已停容器（group、generation、Docker ID、原始对话）查数据库，`SUPERSEDED_CONTAINER_SQL` 同时满足下面两点才删：

1. **不会再启动。** group 的 generation 已经比容器的大；或者 group 在这台 Host 上处于 `cold`，generation 相同，记的是这个容器或者没有容器（恢复失败留下的）。generation 只增不减，每次启动都要过 generation 的栅栏；`cold` group 拒绝任何启动（Files、Git 等工作区请求也一样），结束 `cold` 的领取会把 generation 加 1，并且只要 group 还记着旧 Host 上的容器，旧 Host 就领不到（3.5）。
2. **数据不靠它。** 新的 generation 已经有了容器（恢复成功过）；或者 group 在 `cold`/`restoring` 中，要从一个仍有记录的检查点（`reported` 或 `verified`）恢复（`RESTORE_POINT_SQL`）。检查点在交出时已经核对过完整覆盖这个容器（`COVERED_GROUP_SQL`）。

删除已经完成的 group（`deletion_state = deleted`）不需要保留任何东西，它留在各台机器上的容器都可以删。两条正式删除路径都会把 group 同时设成 `lost` 和 `deleted`，所以这一条要先于下面对 `lost` 的排除单独判断。

一律不删：

- 没删除的 `lost` group：恢复一再失败，留下的容器可能是唯一完好的副本，由管理员决定；
- 正在删除的 group（`pending`、`archived`），交给删除流程；
- group 自己的当前容器、运行中的容器、数据库认不出来的容器、原始对话对不上的容器。

这些都会留下并照旧拦住释放，没有强制绕过。

释放前的依赖检查（`_retirement_impact`，先在释放开始时查一次，提交释放时在锁内再查一次）对已交出的 group 用同一个标准：`cold` 并且有上面那样的恢复点（`HANDED_OVER_GROUP_SQL`），就不算"这台机器还留着 group"。它的旧容器由 8.4 删掉，删没删掉由随后的清点确认。没有可用恢复点的 `cold` group、没交出的 group 照旧拦住释放。之前依赖检查把所有 `cold` group 都算进去，从没人再发消息的 group 永远走不到清理那一步。

### 8.3 删除授权先写进数据库

8.2 的第一个条件一旦成立就不会再变回去：generation 只会变大；`cold` 只会被别的 Host 领走（generation 加 1）或删除；续期、取消移除、Host 重新上线都不会把已交出的 group 还给旧容器。

第二个条件会变：group 最后一次恢复失败就变成 `lost`，旧容器可能又成了唯一完好的副本。查完数据库、调用 Host 之前，group 可能正好变成 `lost`，而 Host 只核对自己的容器、generation 和是否空闲，不知道数据库里的变化。所以不能拿一次普通查询的结果直接去删。

现在每次删除前先记一条授权（`agent_superseded_container_removals`，迁移 `0089_superseded_removals`）：哪台 Host、哪个 group、哪个 generation、哪个容器、原始对话、请求 ID、哪个释放操作、授权时间，Host 确认删掉后再记删除时间。授权在一个事务里完成：先按 group ID 顺序给这些 group 的行加共享锁（`FOR SHARE`），再按 8.2 判断，符合的写入授权。group 变成 `lost` 要改这一行，所以两者只能一先一后：

- 先变成 `lost`：授权时看到 `lost`，不批准，容器留下；
- 先授权：变成 `lost` 要等授权提交。授权一旦提交就算数，删除照常进行。从这一刻起 group 只靠它的检查点；之后再变成 `lost`，也不撤回已授权的删除，因为删除可能已经执行了。授权表里留着记录，管理员可以查到旧容器是什么时候、被哪个释放操作授权删除的。

重试时，Host 还列着的、之前已授权的容器，照原来的请求 ID 继续删，不再重新判断；新出现的容器才重新判断。

还没确认删掉（`removed_at` 为空）的授权，就算 Host 已经不再列出那个容器，也照原来的请求 ID 再发一次。Host 可能已经删掉了容器，只是回执在路上丢了：下一轮清单里没有它，要是只看清单，这条授权就永远收不了尾，删除计数也会漏掉。Host 删除前已经把请求记盘，重发同一个请求会拿到同一张回执；容器早已不在时，Host 证明它不在之后照样完成。拿到回执、记下删除时间，释放才继续。

Host 端用管理员删除已有的 `admin-delete/discard`：按 generation 和确切的容器 ID，要求容器已停、group 空闲，删除前把授权记录写盘。请求 ID 由 Host、group、generation 和容器 ID 算出，重试一定是同一个请求；Host 已经删掉但响应丢了，重试会认出它；容器已经不在了，Host 证明它确实不在之后照样完成。Host 端不需要改代码。

### 8.4 什么时候删

只在释放流程的 `checking_retention` 这一步，清点之前：

1. 对每台 Host 要 Sandbox Manager 的清单，选出已停的容器，按 8.3 授权；
2. 逐个删除已授权的容器，Host 确认后记删除时间，日志记 `superseded_container_removed`；
3. 删完重新要清单；还剩任何容器就照旧以 `retained_groups_present` 停下，再做 SSH 全机扫描（见 8.5）。

操作结果里的 `superseded_removed` 是这台机器各 Host 上已确认删除的容器数，每处理完一台 Host 就从授权表重新数一遍，不在内存里累加。一批里前面的删掉了、后面的临时失败，下一轮重试时前面那些也不会漏计。

Sandbox Manager 用的是控制面自己的令牌，不受 Host 凭证吊销影响，所以已移除、已吊销的 Host 也能清理。Host 没回应、正忙或过载（408、409、425、429、5xx、连不上）时，释放进入等待，30 秒后用同一个请求重试；Host 拒绝请求本身（其他 4xx）或回执对不上时，释放停下等管理员。清单要不到时保持原来的做法：不推断为空，交给 SSH 扫描。

### 8.5 SSH 全机扫描只认控制面记过的回执

释放最后一步的 SSH 全机扫描检查 `/var/lib/cloud-agent/` 下的所有文件，原来只放过本 Host 的 `credential` 和 `.enroll.lock`，其他一律算残留状态，拒绝释放。这条规则早于检查点和归档功能，之后没改过。Sandbox Manager 的归档状态目录就挂在 `/var/lib/cloud-agent/<host>/sandbox-archives/` 下，里面有两类正常记录：

- 检查点回执 `checkpoints/<sha256(组 ID)>.json`：Host 每打一次检查点就原子替换这个组的最新回执，从不删除。旧机器上的组正是靠检查点交出去的，所以只要打过检查点，这台机器原来就过不了扫描。回执只是 S3 上检查点的元数据，数据本身在 S3；
- 删除回执 `<sha256(请求 ID)>/discard.json`、`discarded.json`：8.4 的删除留下的，供同一个请求重试时取回同一张回执。

现在控制面把它记过的完整记录传给扫描脚本（`RecordedHostState`），脚本逐个文件、逐个字段核对，全部对上才放过：

- 检查点回执：文件名是某个组 ID 的哈希；控制面记录的、这台 Host 给这个组打的最新一个检查点（`agent_sandbox_checkpoints`，按 `captured_at` 取最新）里的每个字段——检查点 ID、组 ID、generation、容器 ID、基础镜像、运行时及版本、存储对象的 key 和版本、sha256、大小、层数、格式版本、`start_seq`——回执里都必须有，而且值完全相同。缺字段、改了任何一项都不放过；
- 删除回执目录：目录名是一个已确认删除（`removed_at` 已填）的请求 ID 的哈希，目录里恰好只有 `discard.json` 和 `discarded.json`，而且两个文件都必须正好是 Host 的删除服务为这个请求写下的样子：身份是授权表里这条记录的原始对话、请求 ID、generation、容器 ID，且不带归档对象。`discard.json` 要么记着被删容器的规格（组 ID、generation 与记录一致），要么记着"目标已经不在"和组 ID；`discarded.json` 与之对应。多一个字段、少一个字段、任何一项不同都不放过。

这些记录随 Host 的使用历史一直增长，所以走扫描脚本的标准输入，不放进 SSH 命令行；命令行长度固定，不会因为记录多而超出系统对单个参数的长度限制。

其余一律照旧算残留：控制面没记过或不是最新的检查点回执、错放在别的组名下的回执、写到一半的临时文件、只有 `discard.json` 的未完成删除、目录里多出的任何文件（比如 `archive.tar`）、符号链接、别的 Host 的目录、管理员"先归档再删"留下的目录，以及任何认不出的文件。没有按目录整体放行。

已知限制：管理员删除（先归档再删，或直接丢弃）完成后在 Host 上留下的请求目录，扫描仍然算残留，用过管理员删除的机器释放会被挡住。这是原来就有的问题，本 PR 不处理，后续按同样的方式（控制面传入已完成的删除请求，逐个核对）另做。

### 8.6 机器没了，停下的释放自己收尾

释放停下等管理员（`blocked`）以后，机器到期被服务商回收，原来什么都不会再推动这个操作：分配已经 `terminated`，操作却一直停着，`release_committed_at` 为空。现在保存 `terminated` 或 `absent` 的观测时，如果分配已在释放（`desired_state = absent`）且还没提交，就把它停下的释放操作重新排队；再跑时发现资源已终止，直接提交释放并成功（`_settle_terminal_retirement`）。池成员不在此列，它们由预留的回收流程收尾。

### 8.7 不做的

- **删除前不去 S3 再查一次检查点。** 控制面现在不读检查点所在的存储桶，要查得给它新的权限。机器到期时这些容器同样会随机器消失，也不查 S3；提前清理和到期消失一样安全。检查点的保留和删除（后续）要遵守：`cold`、`restoring` group 的恢复点不能删。
- **不定期清扫。** 只在释放时清理。长期在线的机器（例如恢复失败留下容器的目标机器）上的旧容器以后另做。
- **池成员提前释放**（3.7）。

### 8.8 验证

- PostgreSQL（`tests/integration/storage/test_superseded_containers.py`）：交出后一直没人发消息可以删；只认确切的 group、generation、容器和原始对话；仍在原 Host 的 group、没有可用检查点、正在删除、`lost`、新 generation 还没有容器的都不删；恢复中、恢复后答案不变；恢复失败留下的容器可以删，直到 group `lost`；走正式删除流程完成的 group 可以删。终止观测让停下的释放重新排队，其他操作不动。
- PostgreSQL，授权：先 `lost` 不批准；先授权的在 `lost` 之后仍然有效、按同一个请求 ID 重发；没确认的授权在 Host 不再列出容器后照样返回，确认后不再返回；授权进行中时最后一次恢复失败要等它提交；`lost` 进行中时授权要等它提交，然后不批准。
- PostgreSQL，完整流程：用真实的 worker 申请机器，group 正式交出，从此不创建任何后续聊天任务，主动释放删掉旧容器并成功。删除由 Host 的删除服务执行并写下回执，检查点回执由 Host 的检查点存储写下，SSH 全机扫描运行正式脚本；Host 删掉了容器但回执丢失时，释放先等待，下一轮清单为空，仍按同一个请求 ID 取回回执、记下删除时间后才成功；没有可用恢复点的 `cold` group 仍然拦住释放。
- 单元（`tests/unit/test_compute_host_bootstrap.py`）：正式扫描脚本只放过与控制面记录逐字段一致的检查点回执，以及已确认删除的两种合法回执（删除了容器、证明目标已不在）。未记录、不是最新、任何字段不同、缺字段、错放名下、未完成、多出文件、只改其中一个文件、请求或身份对不上、规格指向别的组、管理员归档目录，都算残留。记录经真实 `OpenSshTransport` 子进程的标准输入传入，5000 组记录时命令行长度与 1 组时相同。
- 单元（`tests/unit/test_compute_reconcile.py`）：一批里前面的删掉、后面的临时失败，重试后计数完整；删完继续释放；还用得着的不删并停下；运行中的容器不送去删；不回应时等待并用同一个请求重试；拒绝或回执不符时停下；删完仍在则停下。
- 改动前，核心用例（删完继续释放、不回应时等待重试、终止后重新排队）失败，改动后通过。
- 第二次复查（2026-10-20）指出：删除生效、回执丢失后，下一轮清单为空就直接跳过，授权永远收不了尾。改为每轮都取回这台 Host 上没确认的授权（见 8.3）。
- 第四次复查（2026-10-20）指出：回执只核对了 ID，改了 generation、容器或存储对象，甚至删到只剩 ID 都会放过；记录塞进命令行，历史多了会超出参数长度上限（实测 1000 组约 160 KB，`E2BIG`）。改为逐字段核对完整记录，记录走标准输入。端到端测试改用数据库里同一份检查点记录写磁盘回执；用真实的 SSH 传输和子进程边界测了 5000 组记录。
- 第三次复查（2026-10-20）指出：删除回执和检查点回执让 SSH 全机扫描拒绝释放（8.5）；一批里部分成功后重试会漏计。改为扫描只认控制面记过的回执，计数从授权表重新数。测试用 Host 自己的删除服务和检查点存储往临时目录真实写文件，再用正式扫描脚本检查，一路跑到释放成功；放过的每个条件逐一去掉时都有测试失败。
- 复查（2026-10-20）指出三处问题：依赖检查挡住从没人再发消息的 group、查完后变成 `lost` 的 group 仍会被删、正式删除完成的 group 删不掉。上面的修正逐一撤回时，都有对应的测试失败；8.2 每个条件逐一去掉时也是如此。
- 真机：第三轮验收的机器已经到期，需要在下一轮里移除机器、交出 group，然后主动释放，确认旧容器被删、释放成功。

## 附：长时间失联的 Host

机器突然死掉（崩溃、断网），CloudLab 也没有报"已终止"时，原来没有人处理：Host 在漏掉 3 次心跳后被标成 `offline`，但它的 group 一直绑在它身上，要等它回来或管理员移除。

现在，Host 连续 `SILENT_HOST_HANDOVER_SECONDS`（30 分钟）没有心跳时，它上面**已停止、检查点覆盖、没有正在运行的一轮**的 group（`SILENT_HOST_GROUP_SQL`）可以被别的能恢复的 Host 领取。

- **领取时才交出。** 没有后台任务提前把 group 转成 `cold`。claim 多了一个分支：别的 Host 领到这个 group 的下一轮时，在同一个事务里锁住 group 行，用 `take_silent_host_group` 把它转成 `cold`，再照 #658 改绑、恢复。没有人来领，group 就一直不变。
- **原机器回来照常继续。** 只有一台机器、或者别的机器都领不了时，原机器回来后 group 仍是它自己的 `stopped` group，按原来的分支领取，用自己的容器继续。之前先转 `cold` 的做法会在这里卡死：`cold` 分支拒绝仍记着旧容器的 Host（3.5），消息会一直排队。
- **领不到 slot 就撤回。** 交出放在一个 savepoint 里；之后如果这次领取拿不到 slot（比如凭证已失效），回滚到 savepoint，group 留在原机器上。
- **不撤销凭证：** 它可能只是网络断开，是否真的不要它由管理员决定。group 被别处领走后它若回来，旧的 generation 会被拒绝（#658 6.7）。
- **已领取的一轮先处理：** 条件里排除有正在运行的一轮的 group（和腾空的 `live_host` 相同）。失联前领走的一轮要等 reaper 把它重新排队，group 才能被领走。
- **范围：** 失联时还在运行、或没有覆盖检查点的 group 不动，它们的最新状态只在那台机器上。缩小这部分损失需要更频繁的检查点，另行评估。
