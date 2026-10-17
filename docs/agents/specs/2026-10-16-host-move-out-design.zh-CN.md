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
- 腾空完的成员仍等租期结束，由已有的终止释放流程收尾。提前释放需要放宽释放前的容器清点（旧 Host 上还留着已搬走 group 的停止容器），这一版不做。

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

- 腾空完的池成员提前释放（见 3.7）。
- 心跳超时自动判定 Host 失联。

## 7 验证

- 单元：窗口计算、inspection 按窗口请求 hold、续期提前、告警只发一次、池容量计算、清扫任务。
- PostgreSQL：检查点未开启或原 Host 不捕获时不腾空、目标有 `new_groups` 限制时不算目标、两台同时进入窗口最多一台腾空、开始后目标消失仍继续；检查点到达即交出、清扫停止闲置 group 后交出、忙碌 group 不停、旧 Host 领不回自己的 group、provider 终止交出 group。
- 检查命令：`make check`、`make test-db`（独立测试库）、`make ci-database`、`make docs`。
