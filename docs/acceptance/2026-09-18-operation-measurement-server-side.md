# PR #258 服务端操作测量验收

日期：2026-09-18。被测分支：`codex/perf/startup-measurement-switch`，目标 `dev`。
状态：**服务端切片完成并通过本机验收；浏览器 producer、跨 replica 全局采样与自动保留
清理仍未实现，本文不把它们算作已验收。**

本文只记录实际跑过的检查与实验。凡是没跑过的，写“未验收”并说明缺什么。

## 1. 交付范围

一套默认关闭、按实验范围/期限/预算开启的服务端性能测量系统：

| 操作 | 起点 | 终点 | 覆盖标签 |
| --- | --- | --- | --- |
| `attempt.startup` | runner 发起 claim 之前 | prompt 交给 supervisor 之后 | `host_startup` |
| `files.list` / `files.open` | CP 完成认证/归属/entitlement/broker 解析 | 业务结果校验完成 | `server_request` |
| `terminal.open`（list/create/stream_attach 各一条） | 同上 | 同上 | `server_request` |

完整流程：配置实验 → 正常执行操作 → 分阶段计时 → 后台有界持久化 → Admin 查看原始
样本和分布 → 到期或关闭后停止采集。

## 2. 本轮修复

### 2.1 `new_group` 启动路径此前永远不可能出现

`ChatSandboxManager.create` 成功后写入的字面量是 `"created"`，而
`SandboxActionFacts.note` 的“最强事实”守卫和 `runner._classify_startup_path` 都判
`"create"`。因此守卫不生效，紧接着必然执行的 `start`（或 `reuse`）覆盖了它，**每个新建
group 都被记成 `resume_stopped`**；四条声明路径实际只剩三条，且 `new_group` 与
`resume_stopped` 混在一起。测试替身写的是 `"create"` 并注释“与真实 manager 一致”，
所以单元测试一直通过——这是测试替身与生产字面量漂移，只有把真实 `create` 交给真实
分类器才能发现。

修复：`cloud_agent_host/chat_sandbox.py` 写入分类器认识的值；新增回归测试
`tests/unit/test_chat_sandbox.py::TestSandboxActionFacts::test_a_real_creation_reports_the_action_the_classifier_reads`
驱动真实 `create` 并把结果交给真实 `_classify_startup_path`，断言 `new_group`。

### 2.2 测试竞争：投递断言先于 JSONL 落盘

`test_the_startup_record_is_delivered_while_the_turn_is_still_running` 在 HTTP 已发送后
立即读异步 JSONL，读到尚未创建的文件。原来靠 200 次 10 ms 轮询“等一等”，那既不是
同步点也不是有界 flush。现在用一个 `threading.Event` 在投递到达目的地的那一刻置位，
断言后再调用 `operation_profiling.flush_sink()`（生产者自己的有界 drain）落盘。
“模型未结束时已投递”的断言原样保留。

### 2.3 Admin 精确筛选与 Runs 接入

- `attempt_id`、`interaction_id` 成为端到端筛选键：port → repository SQL → API →
  typed client → URL/表单 → 记录详情可点击跳转。两者不是同一筛选的两种拼写：
  attempt 选中一次执行（只有 startup 记录带 attempt），interaction 选中一次终端打开
  的三个请求（都没有 attempt）。
- `GET /admin/agent/measurements` 不再静默忽略未知查询参数：返回 422
  `unexpected_query_parameter`。此前 FastAPI 默认丢弃未知键，一次写错键的精确筛选会
  变成整段实验的宽读取而调用方看不出来。
- Admin run detail 的 attempt 明细新增 `StartupMeasurement` 面板，按 job+attempt 精确
  读取本次启动，显示时长、outcome、真实 startup_path、覆盖口径、完整性与每个 partial
  原因、阶段明细，并跳到该 attempt 的完整样本列表。无样本时显示“无已保存样本”并列出
  四种无法区分的原因，绝不显示 0 ms。
- 分布口径的诚实性修正：`incomplete_count` 与 `failed_count` 是两个**条件**而不是两个
  互斥分类，一条样本可以同时属于两者。页面与分组说明现在明确写出“可能重复计数、
  不是已存样本的划分”，不再读起来像一个 partition。

### 2.4 本地栈补上 profiling 透传

`deploy/local/docker-compose.yml` 的 runner/broker 没有 `AGENT_OPERATION_PROFILING_*`，
只有 control-plane 有。本地开实验会**静默**只采到 CP 侧的文件/终端请求，没有任何
startup 记录。已补上三个变量并由 `tests/unit/test_deploy_config.py` 固定。

### 2.5 第 2 轮复核提出的问题

复核提出 4 条，全部成立，已修：

1. **启动基准的计时窗口把埋点排除在外**（§3.3.0）：时钟晚于第一个 mark 启动、早于后续
   mark/`set_attributes`/`finish` 停止，`on-persist` 还在整批结束后才投递，
   `acquire`/`ensure_resident` 也没收到 profiling 上下文。修完附带发现第二个缺陷：
   supervisor 的 dataclass 应答被直接交给只接受 mapping 的 `record_fragment`，于是每条
   记录都带 `missing_fragments` 且 `collection_complete: false`——基准没有采到它声称
   在测的东西。两者都由 `tests/unit/test_operation_overhead_benchmark.py` 固化。
2. **启动明细的 401/403 被吞掉**：`StartupMeasurement` 把 `getMeasurement` 的所有异常
   都转成 `null`，撤权后外层 `onAccessDenied` 收不到，页面继续显示已加载的 Admin 数据。
   现在权限错误直接抛回父级，普通读取失败仍然保留摘要。两条定向测试
   （明细 403、列表 401）断言撤权回调被调用。
3. **CPU 差值不能解释成后台线程成本**（§3.3.0）：两边都是 `process_time()` 的全进程
   CPU，窗口内外混着对端工作、预热与排空。现在只报同等工作量下的整进程 CPU 及其相对
   `off` 的差值，并写明无法按线程归因。
4. **验收汇总与引用的原始输出不一致**（§3.3）：报告把一次运行的数字标成另一次的文件名。
   现在两次运行各自留档为 `runA-overhead.md` / `runB-overhead.md`，报告里每个数字都能
   对回它自己的文件。

## 3. 验收证据

### 3.1 真实链路验收（真实 job/attempt/sandbox/membership + 真实身份 + collector + HTTP + Postgres + Admin）

文件：`tests/integration/servers/test_measurement_acceptance_db.py`（7 个测试，全部通过，
无 skip）。命令：

```bash
TEST_DB_NAME=ca_acceptance_measure_test_db \
  .venv/bin/python -m pytest -q -m dbtest \
  tests/integration/servers/test_measurement_acceptance_db.py
# 7 passed
```

这条链里**真实**的部分：真实 uvicorn 监听（`create_app()` + lifespan，应用自己从
`AGENT_DATABASE_URL` 建池）；真实身份（`identity.mint_session` 签发的 session cookie，
只替换了 `get_gateway_client` 与 `get_agent_app_credentials`，**没有**替换
`get_storage`）；真实 `Storage` 落在真实 Postgres 上；通过真实仓储写入
host/job/claim/sandbox/membership 行（`reserve_thread_sandbox` 加真实
`workspace_ready` 生命周期事件设置 `terminal_ready`）；真实 collector、真实 runner 投递
运行时、真实 worker ingest 路由、真实有界 `measurement_writer`、真实 Admin 路由。run
descriptor 按路由从 Postgres 读出的 sandbox 行做 target 限定。

**仍然是替身的**只有三处，且都是外部进程边界：workspace broker（用真实
`operation_call.open_call`/`BrokerCall` 片段协议应答，只有工作区内容由测试给出）、
session supervisor（挂起的 supervisor/stream）、heartbeat。没有假的 `get_job`/
`get_attempt`，没有存储替身，没有手写的测量行。

| 测试 | 覆盖的验收项 | 断言要点 |
| --- | --- | --- |
| `test_the_startup_record_is_stored_while_the_turn_is_still_running` | 模型仍在运行时启动记录已经入库 | 真实 `_stream_shared_agent_turn` 在活动线程上；`agent_operation_measurements` 里已有该行时 `turn.is_alive()` 且读取线程仍阻塞在 `frames()`；顺序由 event 决定，不靠 sleep |
| `test_a_failed_startup_is_delivered_within_the_bounded_exit_wait` | 启动失败也能投递，退出等待有界 | 在表被 `ACCESS EXCLUSIVE` 锁住时用 `run_turn` 的退出调用与界投递；实测等待 1.6s ≤ 界+1s，`_DELIVERY_DRAIN_TIMEOUTS == 1`；释放后落库，`outcome=failed` 且带 `failure_stage` |
| `test_a_terminal_attach_is_stored_while_the_stream_is_still_open` | SSE 持续打开时 attach 已入库 | 真实 SSE 路由；`terminal.stream_attach` 行存在时 `is_stream_consumed`/`is_closed` 仍为 `False`；`coverage_scope=server_request`、`attempt_id IS NULL` |
| `test_failed_file_and_terminal_requests_follow_the_classification_rules` | 文件/终端失败请求按既定分类规则记录 | 无法定类的非根读取：503、0 行、`rejected==1` 且 `reasons["ambiguous_file_kind"]==1`（绝不冒称 `files.open`）；终端在选中后失败：恰好 1 行 `terminal.open`/`terminal.list`，`outcome=failed`、`collection_complete=False` |
| `test_congestion_keeps_answering_and_the_queue_counts_are_correct` | 真实数据库拥塞下业务继续响应，队列容量/并发/超时/丢弃计数正确 | `LOCK TABLE … ACCESS EXCLUSIVE`；5 个真实文件请求最慢 < 1.0s；`MeasurementWriter(max_count=3, max_concurrent=1)` 下 admin 状态 `pending==2 / in_flight==1 / dropped_full==2 / dropped_timeout==0`；释放后 3 行落库 |
| `test_profiling_off_collects_nothing_and_writes_nothing` | 关闭时没有新增采集任务、投递或测量写入 | `start_operation(...) is None`；同样的请求 200 且响应无 `profiling` 键/头；writer 计数器全 0 且未运行；runner 投递状态全空；`SELECT count(*)==0`；sink 文件从未创建 |
| `test_the_admin_reads_back_the_startup_and_the_whole_interaction` | Admin 按 attempt 与 interaction 精确读回 | `?run_id&attempt_id` 只返回该次启动；`?run_id&interaction_id` 恰好返回 list/create/stream_attach 三条且 `attempt_id IS NULL`，启动记录不在其中 |


### 3.2 测试与门禁

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 快速层 | `pytest -q -m "not external and not dbtest"` | 4700 passed, 595 deselected |
| 数据库层 | `TEST_DB_NAME=ca_startup_measurement_test_db pytest -q -m dbtest` | 543 passed, 4748 deselected |
| 格式 | `ruff format --check .` | 474 files already formatted |
| Lint | `ruff check --no-fix .` | All checks passed |
| 类型 | `mypy` | Success: no issues found in 218 source files |
| Web 单测 | `vitest run` | 102 files, 1641 tests passed |
| Web lint / format / type | `npm run lint`、`format:check`、`type-check` | 全部通过 |
| Web 生产构建 | `next build` | 成功，路由表含 `/admin/measurements` 与 `/admin/runs/[jobId]` |
| OpenAPI 契约 | `pytest tests/unit/test_api_surface_contract.py` | 4 passed（契约已按新增两个查询参数重新生成） |
| 基准窗口回归 | `pytest tests/unit/test_operation_overhead_benchmark.py` | 4 passed（复核第 1、3 条的固化） |
| PR CI | [run 35503608447](https://github.com/HarvardMadSys/hybridInference-cloud-agent/actions/runs/35503608447) | **16 pass / 1 skipped**（`Candidate on real Kata`）；本次复核后的提交另有一次 CI，结果见 PR |

数据库层使用独立测试库 `ca_startup_measurement_test_db`（名称含 `_test_`，fixture 会
拒绝其他名字），跑完自动 truncate 自己创建的行。真实链路验收另用
`ca_acceptance_measure_test_db`。

一次并发跑两个 DB 层级会话时，
`test_legacy_database_copy.py::test_cross_owner_row_rolls_back_archive_and_entire_schema`
失败一次；单独重跑该文件 5 passed，串行重跑整个层级 543 passed。这是两个会话同时动数据库
造成的互相干扰，不是本轮改动；记在这里以免下次把它当成回归。

**CI 在 `dev` 合并结果上失败过一次，原因不在本轮改动。** 本 PR 的 CI 跑的是
head 与 `dev` 的合并结果；期间 `dev` 前进了 #311（`efb77ee`），它从会话列表响应模型里
删掉了 `matching_turn_count` 却没有重新生成
`contracts/agent-control-plane.openapi.json`，因此 `dev` 自己的 CI（run 对应 `efb77ee`）
就是 failure。合并 `dev` 后本分支也复现了同一个 `test_the_served_openapi_is_the_committed_contract`
断言。本分支重新生成了契约（既包含 #311 缺的那 5 行删除，也包含本 PR 新增的两个查询
参数），合并结果因此自洽。这一条要在 PR 描述里写明，避免被读成本轮改动了会话列表。

### 3.3 真实业务开销实验

脚本：`scripts/bench_operation_overhead.py`。第 2 轮复核后重跑两次，两次的完整原始表格按
运行 ID 留档：

| 运行 | 生成时间（UTC） | 原始输出 |
| --- | --- | --- |
| A | 2026-09-20T10:16:20Z | [runA-overhead.md](assets/2026-09-18-operation-measurement/runA-overhead.md) |
| B | 2026-09-20T10:20:17Z | [runB-overhead.md](assets/2026-09-18-operation-measurement/runB-overhead.md) |

两份文件是脚本 `--markdown` 的原始输出，未编辑；下表每个数字都取自这两份文件（百分位也
用它们写入的 `raw_samples_ns` 独立重算过一遍，与文件一致）。本节此前的版本把一次运行的
数字标成另一次的文件名，且引用的文件不是产出这些数字的那一次——这就是本次复核的第 4 条，
现在每个数字都能对回它自己的原始输出了。

**环境**（两次相同）：python 3.13.9、macOS 15.7.7 arm64、14 CPU、PostgreSQL 15.17
(Homebrew, fsync=on, synchronous_commit=on)、Docker 28.4.0（colima/Lima，runc）。
固定单进程、单 replica、串行 cohort、每 cohort 预热 20、每操作 4 轮 × 150 = **n=600**。
百分位用 nearest-rank（Admin 页面用 SQL `percentile_cont`，会插值，两者不会逐位相同）。
预算为设计约定的 `max(10 ms, off_p95 × 1%)`，baseline 为 `off`。

#### 3.3.0 复核后对脚本本身的两处修正

**计时窗口此前把埋点排除在外。** 上一版的时钟在第一个 `mark` 之后启动、在后续的 mark、
`set_attributes`、`finish` 之前停止，`on-persist` 还在整批采样结束后才投递，而且
`acquire` / `ensure_resident` 根本没有收到 profiling 上下文。复核用一个 150 ms 的注入
延迟证明了这一点（返回样本仍是 0.012 ms）。现在：路径准备工作（为该路径创建/停止容器、
为 `reuse_live` 预热 resident）留在时钟之外；时钟覆盖真实启动入口的整段——打开测量、绑定
facts、两次带 profiling 上下文的 supervisor 调用、片段应答、freeze、以及 hand-off 入队。
两个配套缺陷同时修掉：supervisor 的应答是 dataclass，而 `record_fragment` 只接受
mapping，于是每条记录都报 `missing_fragments` 且 `collection_complete: false`——一个没有
采到它所声称要测的东西的基准。现在用 `asdict` 转成 runner 真正收到的线上形状，记录里
`producers` 含 `broker`、无缺失片段、`collection_complete: true`。

回归测试固化在 `tests/unit/test_operation_overhead_benchmark.py`：往 collector 的 mark
里注入已知延迟，断言它在报告的时长里出现；另一条断言 freeze/入队也在窗口内，第三条断言
上下文到达 supervisor 且片段被记为已应答。把时钟改回旧位置，这三条都会失败。

**CPU 差值此前被解释成后台线程成本。** `time.process_time()` 统计整个进程，所以
"请求窗口内 CPU" 与 "其余线程 CPU" 两边都是全进程；窗口里还混着同进程内的对端工作，窗口
外还包含预热、准备与排空。据此算出"每条记录后台消耗 150–200 µs"没有测量依据。现在只报
**同等工作量下的整进程 CPU 及其相对 `off` 的差值**，并在正文与 `caveats` 里写明本平台
（`resource.RUSAGE_THREAD` 不可用）无法把 exporter 线程和 writer 任务从请求路径里拆出来，
差值也不能全部归给某条命名线程。`bare` 每轮多打两组兄弟路由请求，因此只报不比较。

#### 3.3.1 服务端请求（真实 HTTP + 真实 Postgres + 真实 collector/writer）

单位 ms，`bare` 是同应用上未埋点的兄弟路由（写文件 / 终端 resize），不是把埋点从被测
路由里删掉。`Δp95` 为相对同一次运行的 `off`。

| 操作 | 运行 | off p50/p95 | on-persist p50/p95 | Δp95 | on-local Δp95 | 预算 | 结论 |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| files.list.root | A | 2.901 / 7.706 | 3.268 / 6.688 | −1.018 | −1.381 | 10.00 | pass |
| | B | 3.098 / 6.999 | 3.749 / 9.276 | +2.277 | +2.006 | 10.00 | pass |
| files.list.subdir | A | 2.926 / 6.605 | 3.314 / 6.665 | +0.060 | +0.445 | 10.00 | pass |
| | B | 3.417 / 8.539 | 3.750 / 8.757 | +0.218 | **+7.236** | 10.00 | pass |
| files.open | A | 3.006 / 8.294 | 3.296 / 6.805 | −1.489 | −1.721 | 10.00 | pass |
| | B | 3.478 / 7.338 | 4.192 / 12.112 | +4.774 | +4.193 | 10.00 | pass |
| terminal.list | A | 3.363 / 7.029 | 3.599 / 6.227 | −0.802 | −0.381 | 10.00 | pass |
| | B | 3.510 / 8.237 | 4.349 / 13.128 | +4.891 | +3.368 | 10.00 | pass |
| terminal.create | A | 3.282 / 7.555 | 3.683 / 7.883 | +0.328 | +1.557 | 10.00 | pass |
| | B | 3.965 / 10.471 | 4.303 / 7.716 | −2.756 | +2.888 | 10.00 | pass |
| terminal.stream_attach | A | 3.064 / 7.098 | 3.419 / 8.048 | +0.951 | −1.056 | 10.00 | pass |
| | B | 3.932 / 10.614 | 4.267 / 20.359 | **+9.745** | −2.420 | 10.00 | pass |

**六个操作在两次运行里都通过预算**，绝对增量都在 10 ms 下限以内。要说清楚的是：

- 运行 B 的 `terminal.stream_attach` `on-persist` Δp95 是 **+9.745 ms**，只比 10 ms 下限
  低 0.255 ms，同一次运行的 `on-local` 反而是 −2.420 ms。它不是一条稳定的逐操作结论，
  而是"最坏一次观测仍在下限内"，也说明这台共享机器上剩下的余量很薄。
- 运行 B 里 `files.list.subdir` 的 `on-local` Δp95 是 +7.236 ms；运行 A 同一格是
  +0.445 ms。**运行间波动仍然大于多数格子的被测增量**：同一个 `off` cohort 自己的 p95
  在两次之间从 6.6 ms 摆到 8.5 ms（`files.list.subdir`），`terminal.stream_attach` 的
  off p95 从 7.1 ms 到 10.6 ms。因此逐格的正负号大多不能归因于埋点，能说的是"没有格子
  超过预算"。
- `bare` 与 `off` 的 p50 差值同样在噪声内，不能读成"关闭埋点让请求变快"。

#### 3.3.2 四条真实启动路径（真实 Docker 容器 + 真实分类器）

`--startup` 段真实 create/start/reuse 容器，并把真实 collector 包在真实 supervisor
activation 外，计时窗口按 §3.3.0 描述覆盖整段。每次运行每路径每 cohort n=6（预热 1），
每个路径共 24 次真实容器动作：

| 路径 | 真实 container_action | resident_action | 运行 | bare | off | on-local | on-persist |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| `new_group` | `create` ×24 | `new` ×24 | A | 543.8 | 587.2 | 577.3 | 581.9 |
| | | | B | 501.2 | 506.2 | 549.1 | 580.2 |
| `resume_stopped` | `start` ×24 | `new` ×24 | A | 585.5 | 549.8 | 551.7 | 573.1 |
| | | | B | 536.9 | 525.6 | 560.7 | 705.6 |
| `resume_running` | `reuse` ×24 | `resume` ×24 | A | 179.3 | 180.8 | 179.6 | 178.1 |
| | | | B | 171.7 | 184.6 | 187.4 | 193.1 |
| `reuse_live` | `reuse` ×24 | `reuse` ×24 | A | 167.1 | 168.2 | 169.7 | 168.5 |
| | | | B | 159.5 | 168.2 | 174.0 | 180.8 |

数值为 p50（ms）。两次运行的 `recorded_startup_paths` 都恰好是四条路径各 6 条，
`on-persist` 的 hand-off 投递为 `{delivered: 24, dropped: 0, drain_timeouts: 0}`，
Postgres 里 24 行且四条路径各 6 行——即"每条记录在 freeze 点就投递"这条路径本身被验证了。

**这个 n 说明不了尾部**：n=6 时 nearest-rank 的 p95 就是最大值，不是尾部估计，脚本自己
在输出里写明。四条路径的 p50 在 on/off 之间的差值（−5 到 +146 ms）与真实容器动作的
数百毫秒同一量级且方向不一致，**没有一条能读成启动路径的采集开销**——`resume_stopped`
在 B 里的 +146 ms 来自 6 个样本里的单个离群值。真实可用的结论只有一条：埋点新增的成本
落在容器动作的噪声之下。要给出启动路径的尾部结论必须加大 `--startup-iterations`，并把
`execution_profile`/runtime 分组固定下来。

**窗口覆盖埋点这件事不是靠上表证明的，而是靠注入实验。** §3.3.0 的 150 ms 注入让
`on-local`/`on-persist` 的样本从与 `bare` 齐平变成高出约 2 × 150 ms（两个 mark），而
`bare`/`off` 不动——这正是复核用来证伪旧窗口的同一实验，现在作为
`tests/unit/test_operation_overhead_benchmark.py` 常驻。

**这些不是生产数字**：本机是 colima/Lima + runc，无 Kata；边界止于 supervisor acquire +
resident 交接，resident CLI 是替身传输层，CLI 自身初始化不在其中。

#### 3.3.3 丢弃与整进程 CPU

正常负载下零丢弃：两次运行的 `on-local`/`on-persist` 都是 4080/4080 条写入、`dropped_full`
与 `dropped_timeout` 均为 0。

整进程 CPU（`time.process_time()`，含请求路径、collector exporter、控制面 writer，4 轮
各 3600 个请求）：

| 运行 | cohort | 整进程 CPU | 相对 off | 每条记录 | 峰值 RSS |
| --- | --- | ---: | ---: | ---: | ---: |
| A | bare | 12.438 s | —（不可比） | n/a | 174.9 MiB |
| | off | 8.910 s | — | — | 174.9 MiB |
| | on-local | 11.391 s | +2.481 s | +689.1 µs | 174.9 MiB |
| | on-persist | 11.435 s | +2.525 s | +701.4 µs | 174.9 MiB |
| B | bare | 11.714 s | —（不可比） | n/a | 159.3 MiB |
| | off | 9.641 s | — | — | 159.3 MiB |
| | on-local | 13.466 s | +3.825 s | +1062.6 µs | 159.3 MiB |
| | on-persist | 13.270 s | +3.630 s | +1008.3 µs | 159.3 MiB |

即"开启后整个进程在同一批请求上多花 2.5–3.8 s CPU，约 690–1060 µs/条"。这**不是**后台
线程的独立测量：进程内同时跑着客户端与服务端，客户端自身也因多出的记录而多做工作，而且
本平台无法按线程拆分，因此这个差值只能读成"该配置带来的整进程额外 CPU"，不能读成
"exporter + writer 花了多少"。峰值 RSS 两次都没有可测增长（同一 cohort 内前后相同）。
脚本的 `not_measured` 逐条列出了没测到的东西：网络字节（loopback 无便携 per-socket
计数器）、可分线程 CPU、WAL/索引/磁盘字节、容器 CPU/内存。

#### 3.3.4 故意变慢

| 运行 | 场景 | 合成方式 | 业务请求 | 结果 |
| --- | --- | --- | --- | --- |
| A | 慢目的地 | bench-local 落盘每条延迟 20 ms，跨操作累积 | 6 个操作 p95 5.0–7.9 ms | `dropped_full=389`（`queue_count`），sink 1020/1020 条仍写盘；本地目的地 631/1020 |
| B | 慢目的地 | 同上 | p95 7.3–13.0 ms | `dropped_full=281`（`queue_count`），sink 1020/1020；本地目的地 739/1020 |
| A | 慢数据库 | 第二条连接对 `agent_operation_measurements` 持 `ACCESS EXCLUSIVE` | **360 个请求全部应答**，p95 4.4–8.8 ms | `dropped_full=104`、`dropped_timeout=2`；释放后 0.333 s 恢复（预算 22.0 s），254 行落库 |
| B | 慢数据库 | 同上 | 360 个请求全部应答，p95 8.4–12.0 ms | `dropped_full=104`、`dropped_timeout=2`；释放后恢复在预算内，254 行落库 |

两个 arm 都是**合成**的，脚本在报告里标注了合成在哪一层（`slow_sink` 是 bench-local
目的地，不是控制面 ingest；`slow_db` 是 PostgreSQL，由脚本从第二条连接持锁）。它们的
价值在于证明"诊断拥塞只丢样本、不改业务结果"这条设计承诺，而不是给出生产数字。

#### 3.3.5 这次实验**没有**证明的事

脚本的 `not_what` 与 `caveats` 逐条列出，对本 PR 结论最要紧的是：

- 不是端到端延迟：浏览器、网络、host agent 与沙箱都不在任何数字里，没有跑 agent CLI
  或模型。
- 服务端请求的 broker 是 in-process 替身，只讲真实片段协议；文件/终端请求背后没有
  容器工作。
- 服务端操作的 `bare` 是同应用上未埋点的兄弟路由，不是把被测路由的埋点删掉。
- `on-local` 用 bench-local JSONL 替身，因为部署里没有"去掉 Postgres 仓储"的开关。
- 启动路径 n=6，p95 即最大值；真实采集成本落在容器动作噪声之下，本次没有测出启动路径
  的逐路径开销。
- 整进程 CPU 差值无法按线程归因，也包含客户端自身多出的工作。
- 请求速率是持续每几毫秒一次，远高于人类浏览；每轮之间会等有界队列排空。
- 未发送浏览器的 `X-Agent-Interaction-Id` 续接，所以续接多付的那次查找不在样本里。
- `max_operations` 是每进程预算；多 replica 部署会各记各的。

## 4. 明确未验收的部分

| 项 | 状态 | 缺什么 |
| --- | --- | --- |
| 浏览器点击到可见 / xterm 可交互 | 延期 | 没有浏览器侧 producer；现有记录都是 `server_request` 或 `host_startup`，`end_to_end_complete` 恒为 false |
| 纯浏览器缓存命中（不发请求） | 延期 | 与上同：没有请求就没有服务端记录，设计已说明 |
| 跨 replica 全局采样配额 | 延期 | `max_operations` 仍是每进程预算；catalogue 记录每个 admission 实例的预算，Admin 不声称跨副本总上限 |
| 14 天自动保留清理与每-run 20,000 条硬上限 | 延期 | 当前保护是实验预算、64 KiB 单条上限与有界投递队列；页面不声称已有保留策略 |
| 生产 Kata / Linux ARM64 上的绝对启动数字 | 未验收 | 本机是 macOS + colima（runc，无 kata-runtime），与生产隔离不同；不把它当生产数字 |
| 启动路径的逐条开销结论 | 未验收 | n=6 时 p95 即最大值，真实采集成本落在容器动作的数百毫秒噪声之下；要出结论需加大 `--startup-iterations` 并按 runtime/execution_profile 固定分组 |
| 真实 agent CLI 首输出 | 未测量 | startup 边界是 claim → prompt dispatch，CLI 自己产出第一帧在边界之外 |

## 5. 环境

- 主机：macOS（Apple Silicon），Python 3.13.9，PostgreSQL 15.17（Homebrew，127.0.0.1:5432）。
- 容器运行时：colima（Lima VM，aarch64，4 CPU / 6 GiB），Docker 28.4.0，**runc，无 kata-runtime**。
- Python 模块解析：所有 first-party 包（`cloud_agent`、`cloud_agent_host`、
  `cloud_agent_contracts`、`cloud_agent_git`、`cloud_agent_profiling`）及其子模块都解析到
  本 worktree，逐模块核对过；共享 venv 的 editable finder 指向本 worktree。注意
  `.venv` 是共享的，后台任务可能继承另一个 checkout 的环境——本次有一次后台 dbtest
  跑到了另一个 checkout 的 3.12 venv 上并报出 fixture error，改用绝对解释器路径后
  同一层级 543 passed。这条记在这里是因为它会伪装成测试失败。
