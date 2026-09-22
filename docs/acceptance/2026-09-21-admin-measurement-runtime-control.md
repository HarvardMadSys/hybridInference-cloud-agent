# Admin 按需测量：启用、停用和运行时生效 —— 实现与验收

> 2026-09-22 更新：Admin 页面已改为持续采集直到 Disable，移除时长选择和 Details。
> 本文的定时实验说明及旧验收结果保留为历史记录；当前行为见
> [持续采集设计](../specs/2026-09-22-continuous-admin-measurement.zh-CN.md)。


日期：2026-09-21（同日晚修订：按简化后的需求重写）。被测分支：
`murphy/deepseek/admin-measurement-runtime-control`，目标 `dev`，基线 `2aa35f4`（PR #258
合并提交）+ 最新 `dev`（`9c88596`）。设计文档：
`docs/specs/2026-09-21-admin-measurement-runtime-control-design.zh-CN.md`。

本文只记录**实际跑过**的检查。凡是没跑过的写"未覆盖"并说明缺什么；凡是用替身代替真实
组件的写"模拟"，不与实测混列。

## 0. 本次简化删掉了什么

上一版实现把"配置同步"做成了"有期限的可续租授权 + 参与者 ACK 注册表 + 由全体参与者推导
的复合状态"。本轮按新需求删除，**不保留兼容层**：

| 删除 | 原因 |
| --- | --- |
| `RunAuthorization`、15 秒租约、续租序号 | 停用只需停止选取新操作，不需要撤销在途测量 |
| `agent_operation_profiling_participants` 表及 repository 的三个方法 | 不再需要"每个进程是否已应用" |
| `starting` / `collecting` / `partially_active` / `stopping` 状态推导 | 页面报告配置状态，不声称获得全体确认 |
| `stop_in_progress` 停止传播闸 | 授权续租删除后不存在长期旧授权需要等 |
| `min(expires_at, stopped_at + 传播上限) + 10 分钟` 交付窗口 | 停用不撤销在途测量，沿用原有 `expires_at + 10 分钟` |
| 心跳里的 `profiling` 请求/响应字段 | 配置只走专用同步端点，一台机器一条通道 |
| 对应的测试（参与者、ACK、租约、复合状态）与文档章节 | 同上 |

保留并加强的是**配置版本**：`RuntimePolicyStore.publish()` 拒绝比已应用版本更旧的快照，
broker 也拒绝更低 revision 的推送。这是防"旧配置覆盖新配置"的全部机制，没有第二套续租
协议。

## 0.1 复核修复（`52b6505` 复核后的三项）

外部复核在简化版上找到三个真实缺陷，本轮修复并各补了回归证据。**没有恢复短租约、参与者
ACK 或复杂状态机。**

| 问题 | 根因 | 修复 | 回归证据 |
| --- | --- | --- | --- |
| **[P1] 全新 runner 子进程接不上 Admin 选择** | `attach_selection()` 在解析继承 manifest 之前要求 `_load().run` 非空且 run id 相同；真实子进程是新解释器，Admin 模式下没有本地配置 | 子进程**从 context 自带的不可变 manifest 恢复**该次测量，不再要求它自己选中实验；仍强制实验未过期、操作列表合法、不占第二个名额 | `test_a_real_child_process_continues_an_admin_selection`——**真实 subprocess 新解释器**，Admin 模式、无 provider、无静态 descriptor，断言 attached、同 measurement_id、同计时起点、同 run/target、预算未增加，且写出的记录 `profiling_run_id` 正确 |
| **[P2] broker 推送失败后不再同步** | 只在 `changed` 时推送；失败后 revision 已记为 applied，后续轮询不再重发；broker 独立重启同样收不到 | 每次同步都重发当前配置（receiver 按 revision 幂等，同 revision 直接返回不改状态）；推送结果仍只作本地诊断，不构成 ACK | `test_a_failed_broker_push_is_offered_again`（连续两轮共 4 次失败后第三轮送达）、`test_a_restarted_broker_is_given_the_configuration_again` |
| **[P2] Disable 后丢掉在途操作的后续片段** | `start_stage_recording()` 要求 broker 本地配置里的 run 与上下文一致，Disable 后为空即拒绝；于是"记录在、阶段缺" | 区分**延续**与**新选择**：带不可变 manifest 的 context 是延续，按原实验身份继续；只声明操作的 context 仍要求本地配置匹配，调用方不能靠声明覆盖 broker 配置 | `test_a_selected_operation_keeps_its_later_stages_after_a_stop`、`test_a_continuation_cannot_widen_the_operations_it_was_given`、`test_a_context_without_a_manifest_still_needs_the_local_configuration`，以及 HTTP 级 `test_a_configuration_change_does_not_truncate_an_in_flight_request` 与验收链里新增的 post-switch broker 片段断言（`missing_fragments` 不存在，`admission` 阶段在记录里） |

展示收尾（非阻断）：状态词统一为 **Enabled / Disabled**；停用说明写清"停止选择新操作，
已开始的测量会继续完成"；同步提示改为"正常约 30 秒"而非硬保证。

## 0.2 接续边界复核修复（`d9a8c3a` 复核后的两项）

第二轮复核确认上一轮三项正常场景已修好，又找到两处**接续分支的本地边界校验**缺失。同样
在简化模型内修复，没有引入租约或 ACK。

| 问题 | 根因 | 修复 | 回归证据 |
| --- | --- | --- | --- |
| **[P2] 接续绕过部署级 `disabled`** | 接续分支只看 `_load().run`，不看进程的控制模式；`MODE=disabled`（当时也包括未配置的默认值；2026-09-22 起未配置改为 Admin）下仍会分配 measurement 与 recorder | 接续**只在 `MODE=admin` 下可用**：`disabled` 是"本部署禁止采集"，与 Admin 卡片上的 Disable 是两件事；`environment` 继续由自己的静态 descriptor 决定（父子环境相同，走原有 `_load()` 路径） | `test_a_disabled_deployment_refuses_a_continuation`（含真实 `open_call` 解析路径）、`test_a_default_off_deployment_refuses_a_continuation`、`test_environment_mode_still_follows_its_own_descriptor`；subprocess 测试追加"同一份真实 handoff 交给 `MODE=disabled` 的新解释器 → `attached=false` 且 sink 无新行" |
| **[P2] manifest 的 `expires_at` 被解析却未用于截止** | 子进程与 broker 的 deadline 只取顶层 `context.expires_at`；`run.expires_at` 只有在本地 run 恰好同名时才参与取最小值 | 接续时用**原实验的绝对到期时刻**建立本地单调截止，与传输上下文的截止、以及（同名时）本地同实验截止**三者取较早者**；manifest 已过期则直接拒绝 | `test_an_inherited_manifest_that_has_expired_is_refused`（manifest 过期 60 s、顶层还剩 300 s → 两处都拒绝）、`test_the_earlier_of_the_two_deadlines_wins`（manifest 剩 2 s、顶层剩 300 s → 两处剩余均 ≤3 s，而不是 300 s）、`test_a_context_cannot_outlive_the_experiment_it_names`（反向） |

`test_a_configuration_change_does_not_truncate_an_in_flight_request` 更名为
`test_a_disable_does_not_truncate_an_in_flight_request` 并改用 **Admin 模式**驱动——
只有 Admin 控制会在运行中改变配置，而这次改动恰好把这点变成硬约束。断言同时补了反向：
Disable 之后新到的请求不再被选中。

## 1. 交付范围

管理员在 **Admin → Measurements** 顶部的一张卡片上点 **Enable measurement /
Disable measurement**（**同一个按钮**，文字随状态切换），即可控制沙盒启动、终端打开、
文件访问三类服务端采集的开启与停止，**不需要改环境变量、不需要重新部署、不需要重启任何
服务**，到期自动停止，历史与筛选保留。

| 层 | 落点 |
| --- | --- |
| 采集端配置源 | `apps/backend/cloud_agent_profiling/operation_profiling.py`：`AGENT_OPERATION_PROFILING_MODE`、`ControlState`（含 `config_version`）、`RuntimePolicyStore`、可安装的 `PolicyProvider` |
| 控制能力 | `apps/backend/cloud_agent/ports/profiling_control.py`、`domain/profiling_control.py`、`storage/repositories/profiling_control.py`、`application/profiling_control.py` |
| 持久化 | `migrations/versions/0018_profiling_control.py`：控制单例行（含 revision 与停止墓碑）+ 命令幂等表 + run 生命周期列 |
| Admin 接口 | `apps/backend/cloud_agent/api/admin_profiling_control.py` |
| 配置传播 | `workers/profiling_control.py`（CP 每进程快照）、`api/host_profiling.py`（host 配置读取）、`cloud_agent_host/profiling_policy.py`（runner 同步 + broker 下发）、`cloud_agent_host/broker_profiling_policy.py`（broker 接收） |
| 前端 | `apps/frontend/src/app/(agents)/admin/measurements/MeasurementControlPanel.tsx`、`src/lib/api/adminMeasurements.ts` |
| 部署 | `deploy/{control-plane,host,local}` 三个 compose + 两个 `.env.example` + `ENVIRONMENT.md` |

## 2. 使用说明

### 2.1 部署默认行为（2026-09-22 更新）

正常升级 CP、runner、broker 并完成 `alembic upgrade head` 后，无需设置 profiling 环境
变量。Admin 默认可控制；新数据库没有活动实验，页面显示 Disabled 与 Enable measurement。
旧部署的显式 `MODE=admin` 可保留。旧 `ENABLED=true` 保持静态实验兼容；显式
`MODE=disabled` 仍禁止采集。详见 [默认行为设计](../specs/2026-09-22-measurement-admin-default.zh-CN.md)。

只有多个独立部署共享同一个数据库时，才需配置不同的 `AGENT_OPERATION_PROFILING_DEPLOYMENT`。
控制状态持久化，服务重启恢复未到期实验，不会因重启或关闭页面自动停止。

### 2.2 管理员操作

1. 打开 **Admin → Measurements**，页面顶部是 `Measurement collection` 卡片。
2. 点 **Enable measurement**，选择测量对象（默认全选三项）、目标（一台 host 或 All
   hosts，必须显式选择）、持续时间（1 / 5 / 10 / 30 分钟，默认 10），提交。
3. 按钮变成 **Disable measurement**，卡片显示 `Enabled`、选中的操作、`Host:`、
   `Expires at:`、`Time left: mm:ss`、`Stored samples: N`，以及一行同步延迟提示。
4. 期间正常使用产品即可：浏览文件、打开终端、启动沙盒都会产生记录。**测量不会主动启动
   沙盒、打开终端、读取文件或运行模型**；还没有匹配操作时显示明确提示，而不是 0 ms。
5. 点 **Disable measurement** 立即停止**选取新操作**；不弹确认框，不中断任何用户操作，
   不删除数据，已经开始的 measurement 正常完成并入库。
6. 到期自动停止：期限是权威时间，不依赖页面打开或清扫执行；CP 重启和页面关闭不会改变持久化开关。
7. 内部诊断字段保留在 Admin API，页面不展示 Details 区块。

### 2.3 状态与延迟口径

| 展示状态 | 判据 |
| --- | --- |
| `Disabled`（`off`） | 有控制行，且没有未到期的实验；按钮为 Enable |
| `Enabled`（`on`） | 配置里的实验尚未到期；按钮为 Disable |
| `Unavailable` | 部署没有控制行（从未启用运行时控制）；不提供按钮 |
| `Managed by deployment configuration` | 部署未处于 `admin` 模式；不提供按钮 |

页面展示的是**采集配置状态**，不声称已经获得所有进程确认。同步延迟：CP 空闲 30 s / 活动
5 s 轮询，host 同样节奏，单次请求超时 2 s —— 页面说明正常约 30 秒，不作断网时限保证。断联节点
在实验自己的 `expires_at` 停止选取。

## 3. 实测通过（真实组件）

### 3.1 仓库质量门

```bash
.venv/bin/python -m ruff format --check .        # 489 files already formatted
.venv/bin/python -m ruff check --no-fix .        # All checks passed!
.venv/bin/python -m mypy                         # Success: no issues found in 228 source files
.venv/bin/python -m pydocstyle <四个包>          # 无输出
cd apps/frontend && npm run lint && npm run format:check && npm run type-check && npm run test
```

前端四项：eslint 无告警；Prettier 全部符合；`tsc --noEmit` 无错误；`vitest run`
**103 个文件 / 1674 个用例通过**。

### 3.2 全量测试

| 命令 | 结果 |
| --- | --- |
| `pytest tests/unit tests/servers -q` | **4767 passed, 1 failed**（本机 `os.openpty` 被拒，§6.1）+ 1 个需重新生成的 OpenAPI 契约（已生成，重跑 8 passed） |
| `pytest -q -m dbtest`（真实 PostgreSQL 16 容器，Alembic 迁移到 head） | **546 passed, 3 failed**（本轮）/ 上一轮 **549 passed** —— 失败的是同一组到期边界用例，属 base 上已有的时序抖动，见 §6.8 |
| 其中与本功能相关的 DB 用例 | **47 passed**（两个验收链 + measurement store/chain） |

### 3.3 真实链路验收

`tests/integration/servers/test_profiling_control_acceptance_db.py`（6 个用例）。真实
Alembic 迁移、真实 `asyncpg` 连接池、两个真实 FastAPI 应用各自跑在真实 uvicorn socket 上。

1. **启用**：`POST /admin/agent/profiling-runs` → PostgreSQL 创建实验与控制行 → CP 自己的
   同步循环把配置发布到采集器（断言 `source == "admin"`、`config_version == 1`、
   `deadline_mono > now`，且**没有租约**）。
2. **配置真的到了 broker**（本轮新增，修的就是这条）：测试用一个**真实的
   `ProfilingPolicySync`**，配**真实的 `SessionSupervisorClient`** 与**真实 mint 的 host
   凭据**，先打真实 CP 端点、再打**单独 serve 的 broker 应用**。断言：同步返回 5 s 活跃
   间隔、`state()["run_id"] == run_id`、`broker_ok is True`，且 **broker 自己的
   `applied_state()` 里 revision 与 run id 都对**。这条链路在上一版是断的（同步器建的时候
   没接 broker client），所以它现在是显式断言而不是"应该会通"。
3. **真实根入口产生样本**：真实属主会话读一个文件 → 真实 `operation_measurement` 选根 →
   真实 broker 片段协议 → 有界投递 → PostgreSQL；断言落库行的 run/target/operation 与
   `record.control_revision == 1`。
4. **读回**：`GET /admin/agent/profiling-control` 给出 `status == "on"`、
   `stored_samples == 1`、`run_active is True`、`bounds`、`sync_note` 含 "30 seconds"；
   `GET /admin/agent/measurements?run_id=...` 能读到该样本。
5. **幂等**：同 request id、同 body 重试 → 同一个 run、同一期限。
6. **停用**：`POST .../{run_id}/stop` → revision 递增、`run_active is False`、
   `last_stopped_run.run_id == run_id`、`status == "off"`；换一个 request id 再停一次仍是
   同一 revision。
7. **停用之后**：再读一次文件，响应里**没有** `profiling` 块，已入库行数仍为 1。
8. **在途测量照常完成入库**：请求 A 在实验进行时选根并把 block 保持打开，期间用真实 HTTP
   停用，然后 A 走**真实进程内持久化路径** `record_measurement`。断言行已入库、
   `outcome == "completed"`、`collection_complete is True`、没有 `profiling_stopped`。
9. **停用之后发出的 broker 片段仍然收齐**（复核补充）：同一个 block 里，停用之后再用 A 的
   context 调 `start_stage_recording`，断言 recorder 非空、阶段被记录，最终记录里
   `missing_fragments` 不存在且 `admission` 阶段出现在 `stages[].children`。

另外 4 个用例覆盖：第二个启用被 409 `already_active` 拒绝并带回当前 revision；非 Admin
模式返回 409 `not_admin_mode` 且状态 `unmanaged`；未知操作/未知 host/超时长/未知字段在
不移动控制行的前提下被 422；匿名读写 401/403。

### 3.4 真实 HTTP 的接口与配置交换

`tests/servers/test_profiling_control_api.py`（15 个用例，真实路由/依赖/模型/传输）：

- 启用的形状、审计写入、幂等重放、`already_active` / 陈旧 revision 的 409 语义。
- 停用的幂等、`last_stopped_run`、旧页面不能停掉新实验（run id 校验）。
- 没有控制行 → `unavailable`（不是 `off`）；`environment` 模式 → `unmanaged` 且写入 409。
- host 配置交换：回答里有 run/operations/target/`max_operations`/`server_time`，**没有
  `lease`**、不记录任何关于该 host 的状态；**请求体里写另一台机器的名字也不会改变答案**
  （断言 builder 收到的 host 来自凭据而不是请求体）。
- broker 端点：同 revision 幂等、**更低 revision 被 409 拒绝**、没有配置时
  `start_stage_recording` 返回 `None`。

### 3.5 采集器与父子进程

- `tests/unit/test_operation_profiling_runtime_control.py`（26 个用例）：模式解析（未设 →
  旧语义；`disabled`；`environment`；`admin` 忽略旧 `ENABLED/RUN`；拼错会告警并关闭）、
  **停用不再选中新操作但不终止已开始的 measurement**、在途测量按原 manifest 完成并入库、
  实验到期才截断、**更旧配置版本不能覆盖更新的**、重复同步不重置预算、切换实验不串数据、
  `handoff_context` → `attach_selection` 的父子继承契约。
- `tests/unit/test_runner_host_service.py`、`tests/unit/test_agent_runner.py` 保持通过：这些
  用例里 runner 父进程**真实 `fork/exec`** `python -m cloud_agent_host.runner --turn`，
  `_profiling` 经 JSON handover 传给子进程，子进程 `attach_selection` 重建**同一个
  measurement_id、同一计时起点、同一实验配置**，且不重新占名额。

### 3.6 部署配置

`tests/unit/deploy/test_artifact_compose.py`、`tests/unit/test_deploy_config.py` 覆盖：
三个 compose 都把 `AGENT_OPERATION_PROFILING_MODE` 传给 CP、runner、workspace broker，
本地栈的 deployment 命名空间按 `${...:-default}` 透传。

## 4. 模拟通过（替身代替了真实组件）

| 用例 | 真实部分 | 替身部分 | 为什么 |
| --- | --- | --- | --- |
| 3.3 的 broker 片段 | 真实 `operation_call.open_call` / `BrokerCall`、真实 runner↔broker HTTP、真实投递与入库 | 工作区内容由 fixture 提供；没有真实容器 | 本机没有可用的容器运行时 |
| 3.4 的存储 | 真实路由/依赖/模型/传输 | 内存版 control capability | 该文件测 HTTP 语义；SQL 语义由 3.3 的真实 PostgreSQL 覆盖 |
| 3.5 的 admin 服务 | 真实服务类 | 内存版 capability | 同上 |

**没有**把 mock 延迟当生产实测。

## 5. 时限口径（实现事实，非实测延迟）

| 环节 | 值 | 位置 |
| --- | --- | --- |
| CP 配置同步（空闲 / 活动） | 30 s / 5 s | `workers/profiling_control.py` |
| host 配置同步（空闲 / 活动 / 失败重试） | 30 s / 5 s / 10 s | `cloud_agent_host/profiling_policy.py` |
| 单次配置请求超时 | 2 s | 同上 |
| 一次变更在所有进程生效上限 | 30 s（UI 写出该数字） | 同上 + `sync_note` |
| 断联节点停止选取的边界 | 实验自己的 `expires_at` | 采集器 |
| 接续可用的控制模式 | 仅 `MODE=admin`；`disabled`（含默认未配置）拒绝，`environment` 走本地 descriptor | `operation_profiling._manifest_for_continuation` |
| broker 配置重发 | 每次同步（活动 5 s / 空闲 30 s） | `cloud_agent_host/profiling_policy.py` |

**没有** 15 秒租约，也**没有** 20 秒全量失效承诺 —— 那套机制已随本次简化删除。

## 6. 未覆盖 / 环境限制

1. **`tests/unit/test_chat_sandbox.py::test_retained_terminal_keeps_the_shell_in_the_docker_tty_process_group`
   在本机失败**：`os.openpty()` 被本机沙箱拒绝（`PermissionError: [Errno 1]`），与本 PR
   无关（该文件未被修改），CI 上通过。
2. **没有真实 Docker/Kata 容器参与验收**：`attempt.startup` 的 broker 子阶段只有单元与
   路由级证据。
3. **没有跨 CP replica 的真实并发验证**：控制行的 `FOR UPDATE` + 事务级 advisory lock 保证
   互斥，有真实 PostgreSQL 的顺序 409 路径，但没有两个 replica 同时启用的实测。
4. **没有真实网络分区实测**：断联后的停止由"实验 `expires_at`"这一确定性边界保证，单元
   测试覆盖；不是拔网线验证。
5. **浏览器截图未附**：前端由 vitest（103 文件 / 1674 用例）覆盖，但没有在真实浏览器里
   点过 Enable/Disable。
6. **FI staging 配置未改**：见 `docs/acceptance/2026-09-21-admin-measurement-runtime-control-fi.md`。
7. **没有性能基准**：只给出轮询频率与量级，没有跑 `scripts/bench_*`。
8. **`dbtest` 全量有 5 个到期边界用例会间歇失败**（attempt ledger reconciliation ×2、
   MCP 连接 deadline ×2、source control OAuth state ×1）。它们用真实时钟断言"某个到期
   瞬间之后不能再使用"，在本机负载下会跨过边界；其中两个已在**未打本 PR 的干净
   `9c88596` worktree** 上复现同样失败。跨轮次结果是 549 passed / 546 passed 交替，
   **与本功能无关的领域**（attempt ledger / MCP / OAuth），本功能相关的 DB 用例
   47 个稳定通过。

## 7. 已知取舍

1. **停用不撤销在途测量**（本轮明确要求）。代价：停用之后仍可能有已开始的样本入库，最多
   持续到该 measurement 自己的采集边界。
2. **停止传播没有硬时限**：页面写"最多 30 秒"，实际取决于同步节奏与实验期限。这是本轮
   刻意接受的取舍，不再用租约把上限压到 15/20 秒。
3. **`last_stopped_run` 只保留最近一次**：更早的实验在 run catalogue 里，控制卡不追溯。
4. **`All hosts` 在 API 层是空 target**：新加入的 host 也会被要求采集。
5. **控制表多一行 `deployment=''` 哨兵行**：命名了部署却没有该行时读写落到这一行，第一次
   启用会写入部署自己的行；代价是多一行哨兵数据。
6. **每个 root 生产进程额度仍是 100（部署级硬上限 1000）**，没有全站精确上限；API 返回
   `budget_scope=producer_instance`。

## 8. 证据摘要

```
$ .venv/bin/python -m ruff format --check .
489 files already formatted
$ .venv/bin/python -m ruff check --no-fix .
All checks passed!
$ .venv/bin/python -m mypy
Success: no issues found in 228 source files
$ .venv/bin/python -m pydocstyle apps/backend/cloud_agent apps/backend/cloud_agent_host \
      apps/backend/cloud_agent_profiling apps/backend/cloud_agent_shared
(无输出)

$ PYTHONPATH=apps/backend:. pytest tests/unit tests/servers -q -p no:cacheprovider --timeout=300
2 failed, 4767 passed, 5 warnings in 324.09s
  ├─ FAILED tests/unit/test_api_surface_contract.py::test_the_served_openapi_is_the_committed_contract
  │    （契约文件已按新接口重新生成，重跑 8 passed）
  └─ FAILED tests/unit/test_chat_sandbox.py::test_retained_terminal_keeps_the_shell_in_the_docker_tty_process_group
       PermissionError: [Errno 1] Operation not permitted（os.openpty，见 §6.1）

$ PYTHONPATH=apps/backend:. TEST_DB_PORT=55432 pytest -q -m dbtest -p no:cacheprovider --timeout=300
549 passed, 4837 deselected, 697 warnings in 401.86s (0:06:41)

$ PYTHONPATH=apps/backend:. TEST_DB_PORT=55432 pytest \
      tests/integration/servers/test_profiling_control_acceptance_db.py -q -m dbtest
6 passed

$ PYTHONPATH=apps/backend:. pytest tests/servers/test_profiling_control_api.py -q
15 passed

$ PYTHONPATH=apps/backend:. pytest tests/unit/test_operation_profiling_runtime_control.py \
      tests/unit/test_profiling_control_application.py \
      tests/unit/test_agent_operation_profiling.py -q
157 passed

$ cd apps/frontend
$ npm run lint          # 无告警（--max-warnings 0）
$ npm run format:check  # All matched files use Prettier code style!
$ npm run type-check    # 无错误
$ npm run test          # Test Files 103 passed (103) / Tests 1674 passed (1674)
```

未跑：`scripts/bench_*`（未做性能基准）、`pytest -m external`（需要外部服务）。

### CI

简化后的分支 CI 全绿（16 个提交，最终 `350492e`）：

```
CI Gate pass · Classify Changes pass · Gitleaks pass · Compose resolves pass
Lint pass · Tests pass · Tests (Postgres) pass · Web pass
control (arm64) pass · runner (amd64) pass · sandbox (amd64) pass · egress (amd64) pass
tunnel (amd64) pass · web-agents (arm64) pass · web-root (arm64) pass
Candidate on real Kata: skipping（未触发）
```

`Tests (Postgres)` 在 CI 上同样是绿的，与本机 `dbtest` 全量 549 passed 一致；`Tests` 绿也说
明 §6.1 的 `os.openpty` 失败只发生在本机沙箱。
