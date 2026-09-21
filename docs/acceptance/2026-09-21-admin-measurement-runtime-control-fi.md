# FI 配套项：staging 切到 Admin 测量控制

日期：2026-09-21。配套 PR：
`HarwardMadSys/... → murphy/deepseek/admin-measurement-runtime-control`（目标 `dev`）。

Cloud Agent 仓库的改动本身不修改 FI 的环境配置。按设计文档 §7 第 4 条，本文列出 **FI
侧需要做的修改**，以及**本 PR 不做**的部分，避免"只改了 Agent 仓库就宣称 staging 可点"。

## 1. 必须做：把三个采集进程切到 Admin 模式

`AGENT_OPERATION_PROFILING_MODE` 必须同时出现在 CP、runner、workspace broker 三个进程
的环境里。runner 和 broker 是**两个不同的容器**：只给 runner 会让 broker 拒绝所有片段，
记录会显示 `collection_complete=false` 且 `missing_producers=["broker"]`——采集"能用"但
样本是残的，这正是设计文档 §2 表格最后一行警告的那件事。

```env
# staging 的三处（CP / runner / workspace broker）
AGENT_OPERATION_PROFILING_MODE=admin

# 如果 staging 与其它部署共用同一个数据库，各自命名；单部署可留空（默认 default）
AGENT_OPERATION_PROFILING_DEPLOYMENT=staging
```

- `deploy/control-plane/.env`（CP）
- `deploy/host/.env`（runner 与 workspace broker 同源，compose 两个 service 都要透传；
  本 PR 已在该 compose 文件里加了对应的 `environment:` 行，FI 只需给值）

旧变量不需要改，也不需要删：`admin` 模式下 `AGENT_OPERATION_PROFILING_ENABLED` 与
`AGENT_OPERATION_PROFILING_RUN` 不再决定是否采集，旧的 `RUN` 不会在 Stop 后被当作
fallback。

## 2. 必须做：迁移

```bash
alembic upgrade head    # 新增 0018_profiling_control
```

升级是纯增量的（新表 + 新列 + 两个索引 + 一行哨兵数据），不会改写任何已有 manifest。
`downgrade` 会删表并回退列；**只在采集已停止时降级**。

## 3. 必须做：一次性的开关验证

切 mode 需要重启一次（这是 `MODE` 的定义，不是每次实验的开销）。重启完成后验证：

1. 打开 **Admin → Measurements**，卡片显示 `Off` 与 `Start measurement`（不是
   `Unavailable`）。看到 `Managed by deployment configuration` 说明某一个进程的
   `MODE` 没生效。
2. 点 **Enable measurement**，选 1 分钟、All hosts、三项全选。按钮应变成
   **Disable measurement**，状态变 `Measuring`；**允许最多 30 秒**让配置同步到 CP、
   runner、broker（页面就写着这个数）。
3. 期间做一次文件浏览、一次终端打开、一次沙盒启动，`Stored samples` 应增长。若一直是 0，
   先确认这些操作确实发生过（空状态提示说的就是这个）。
4. 点 **Disable measurement**，确认状态变 `Off` 且 `last_stopped_run.run_id` 是刚才那个
   实验；随后再浏览一次文件，`Stored samples` **不应**增长。
   注意：停用**不会**取消已经开始的测量，所以停用前一刻发起的请求仍可能入库——这是预期行为。
5. 确认 Enable/Disable 期间 CP / runner / broker 的 PID 或容器**没有重启**（`docker ps`
   的 `STATUS` 不变）。若开关触发了重启，说明配置接线错了。

## 4. 不要做

- **不要**用开关去触发 CI、deploy workflow 或改 GitHub Secrets；本功能不碰这些。
- **不要**把 host 凭据给 broker 或沙盒。配置是从 runner 经 broker 已有的共享令牌通道下发
  的；给 broker 加 host 凭据是设计明确禁止的扩张。
- **不要**在未授权的情况下开生产采集。本 PR 只在本地/测试部署验收，staging 按后续
  授权推进，生产不在范围内。

## 5. 回滚

```env
AGENT_OPERATION_PROFILING_MODE=environment   # 或删掉这一行 = legacy 语义
AGENT_OPERATION_PROFILING_ENABLED=false
```

重启三个进程即回到旧行为：进程重新读自己的环境，不再轮询控制行。控制行与历史样本保留在
数据库里，Admin 页面继续可读（显示为 `Managed by deployment configuration`）。

## 6. 需要在 FI 确认的两个问题

1. **staging 是否有多个 CP replica？** 每个 replica 都跑自己的同步循环。多 replica 下
   `Stored samples` 是数据库真实计数（正确），但 `max_operations` 仍是**每进程**额度——
   页面与 API 都写了 `budget_scope=producer_instance`，不要按全站上限理解。
2. **数据库是否与其它部署共用？** 如果是，`AGENT_OPERATION_PROFILING_DEPLOYMENT` 必须
   每个部署取不同值，否则两个部署会看到并可能停掉对方的实验。
