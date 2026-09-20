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

修复：`apps/backend/cloud_agent_host/chat_sandbox.py` 写入分类器认识的值；新增回归测试
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
   现在每次运行各自留档，报告里每个数字都能对回它自己的文件。

### 2.6 第 3 轮复核提出的问题

复核又提出 3 条，同样全部成立，已修：

1. **启动基准阻塞了它自己的接收端**（§3.3.0）：uvicorn 与 activation 循环同处一个事件
   循环，整批同步执行到 cohort 结束，后台发送线程的 HTTP 请求在整个 cohort 期间无人应答
   ——`on-persist` 因此没有模拟"启动期间正常运行的持久化链路"，样本量一大还可能人为触发
   投递超时。改为 `asyncio.to_thread`，并让脚本在每个 cohort 内采样 Postgres 行数、
   cohort 结束前一条都没收到就硬失败。两次运行都是 `rows_visible_before_cohort_end = 23`。
2. **每条记录 CPU 的分母漏了预热请求**（§3.3.0）：窗口含 4080 个请求却除以 3600，
   系统性高估 13.3%。分母改由 `_requests_in_window` 给出并有单测固定，输出里印出该数字。
3. **汇总把两个 cohort 的数据对调**（§3.3）：`terminal.stream_attach` 的 on-local 列被写
   进了 on-persist 列。本轮起所有性能数字都由运行 JSON 直接生成后粘贴，不再手写。

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
| 快速层 | `pytest -q -m "not external and not dbtest"` | 4710 passed, 595 deselected |
| 数据库层 | `TEST_DB_NAME=ca_startup_measurement_test_db pytest -q -m dbtest` | 543 passed, 4762 deselected（库重建后） |
| 格式 | `ruff format --check .` | 474 files already formatted |
| Lint | `ruff check --no-fix .` | All checks passed |
| 类型 | `mypy` | Success: no issues found in 218 source files |
| Web 单测 | `vitest run` | 102 files, 1641 tests passed |
| Web lint / format / type | `npm run lint`、`format:check`、`type-check` | 全部通过 |
| Web 生产构建 | `next build` | 成功，路由表含 `/admin/measurements` 与 `/admin/runs/[jobId]` |
| OpenAPI 契约 | `pytest tests/unit/test_api_surface_contract.py` | 通过（契约已按新增两个查询参数重新生成） |
| 基准回归 | `pytest tests/unit/test_operation_overhead_benchmark.py` | 5 passed（第 2 轮第 1/3 条与第 3 轮第 2 条的固化） |
| 镜像分类器 | `pytest tests/unit/deploy/test_artifacts_workflow.py` | 21 passed（见下） |
| PR CI | [run 35510697218](https://github.com/HarvardMadSys/hybridInference-cloud-agent/actions/runs/35510697218) | **16 pass / 1 skipped**（`Candidate on real Kata`），含 Tests 2m42s、Tests (Postgres)、Web、Lint、Gitleaks、Compose、四个镜像构建与 `CI Gate` |

数据库层使用独立测试库 `ca_startup_measurement_test_db`（名称含 `_test_`，fixture 会
拒绝其他名字），跑完自动 truncate 自己创建的行。真实链路验收另用
`ca_acceptance_measure_test_db`。

一次并发跑两个 DB 层级会话时，
`test_legacy_database_copy.py::test_cross_owner_row_rolls_back_archive_and_entire_schema`
失败一次；单独重跑该文件 5 passed，串行重跑整个层级 543 passed。这是两个会话同时动数据库
造成的互相干扰，不是本轮改动；记在这里以免下次把它当成回归。

同一个文件在负载下还会留下**更难看的余波**，值得单独记一笔：它自己会改
`alembic_version`，一旦失败，共享测试库就被留在 `0007_attempt_turn_resources` 且 schema
已被改过。后续测试再跑 `apply_migrations` 会试图在旧 revision 上叠加迁移，直接撞上
`tables can have at most 1600 columns`。级联表现为十几个不同文件的失败与 error，看起来
像大面积回归。处理方式是 `dropdb` / `createdb` 后重跑——重建后同一次 `-m dbtest` 是
**543 passed**。库里没有本轮代码的问题。

**CI 在 `dev` 合并结果上失败过一次，原因不在本轮改动。** 本 PR 的 CI 跑的是
head 与 `dev` 的合并结果；期间 `dev` 前进了 #311（`efb77ee`），它从会话列表响应模型里
删掉了 `matching_turn_count` 却没有重新生成
`contracts/agent-control-plane.openapi.json`，因此 `dev` 自己的 CI（run 对应 `efb77ee`）
就是 failure。合并 `dev` 后本分支也复现了同一个 `test_the_served_openapi_is_the_committed_contract`
断言。本分支重新生成了契约（既包含 #311 缺的那 5 行删除，也包含本 PR 新增的两个查询
参数），合并结果因此自洽。这一条要在 PR 描述里写明，避免被读成本轮改动了会话列表。

**`dev` 之后又前进了两次，各自暴露/带来一件事。** 第一次是 #314（`22ea8b6`），它暴露了本分支自己的一个漏配。 #314 新增
`test_a_change_to_what_both_images_ship_runs_the_image_jobs`：它从两个 Dockerfile 里推导出
"两边都 COPY 的包"，再断言 CI 的变更分类器会为这些包触发镜像构建。本分支在 `4701934`
把 `cloud_agent_profiling` 加进了两个 Dockerfile 与 Makefile，却没有加进
`.github/workflows/ci.yml` 的 `docker=true` 正则——于是"改了这个包却一个镜像任务都不跑"，
正是那些任务存在的理由。合并 `dev` 后该测试在本机复现，已把 `apps/backend/cloud_agent_profiling/`
补进分类器；`pytest tests/unit/deploy/test_artifacts_workflow.py` 20 passed（`dev` 侧为 21，本分支合并后同一文件为 20 + 本分支内容）。

第二次是 #313（`38cd87d`）的目录重构：`cloud_agent/`、`cloud_agent_host/` 移到
`apps/backend/`，`cloud_agent_contracts/` 与 `cloud_agent_git/` 合并为
`apps/backend/cloud_agent_shared/`，`web/` 移到 `apps/frontend/`，`contracts/` 移到
`gateway_contracts/`。合并后本分支的测量包落到 `apps/backend/cloud_agent_profiling/`，
两个 Dockerfile、`pyproject.toml` 的包与 mypy 列表、Makefile 的 pydocstyle 目标、CI 分类器、
AGENTS.md 的目录图和部署测试都按新路径重新列上；仍留在 `web/` 下的四个测量页面文件移到
`apps/frontend/`。合并后重跑：`uv sync` 后 editable finder 指向新路径，mypy 218 文件、
Ruff/格式、快速层 4709 passed、数据库层 543 passed（新库）、前端 lint/format/type-check 与
1641 个测试全通过。

### 3.3 真实业务开销实验

脚本：`scripts/bench_operation_overhead.py`。第 3 轮复核后重跑两次；下表每个数字都由
`/tmp` 里的运行 JSON 直接生成后粘贴，不经手抄（上一轮就是因为手抄把两个 cohort 的列对调
了）。两次运行的完整原始表格按运行 ID 留档：

| 运行 | 生成时间（UTC） | 原始输出 |
| --- | --- | --- |
| C | 2026-09-20T12:12:52Z | [runC-overhead.md](assets/2026-09-18-operation-measurement/runC-overhead.md) |
| D | 2026-09-20T12:16:42Z | [runD-overhead.md](assets/2026-09-18-operation-measurement/runD-overhead.md) |

```
# run C: 2026-09-20T12:12:52.385124+00:00  docker=28.4.0 overlayfs linux
# run D: 2026-09-20T12:16:42.636897+00:00  docker=28.4.0 overlayfs linux
env: 3.13.9 macOS-15.7.7-arm64-arm-64bit-Mach-O cpus=14 15.17 (Homebrew)
requests_per_cohort: 4080
```

环境（两次相同）：python 3.13.9、macOS 15.7.7 arm64、14 CPU、PostgreSQL 15.17
(Homebrew)、Docker 28.4.0（colima/Lima，runc）。固定单进程、单 replica、串行 cohort、
每 cohort 预热 20、每操作 4 轮 × 150。百分位 nearest-rank（Admin 页面用 SQL
`percentile_cont`，会插值）。预算为 `max(10 ms, off_p95 × 1%)`，baseline 为 `off`。

#### 3.3.0 第 3 轮复核修掉的三处

**启动基准阻塞了它自己的接收端。** uvicorn 与 `_run_startup_section` 跑在同一个事件循环
上，而整批 activation 是同步调用到 cohort 结束才 await，所以后台发送线程虽然启动了，HTTP
接收端在整个 cohort 期间收不到请求。复核用真实 HTTP + 真实 runner 投递复现：两次
activation 返回时接收数都是 0，进入 cohort join 才变成 2。现在每次 activation 走
`asyncio.to_thread`，事件循环可以服务 ingest；并且脚本在每个 cohort 的 activation 之间
采样一次 Postgres 行数，`on-persist` 若在 cohort 结束前一条都没收到就直接失败退出。
两次运行的证据：`rows_visible_before_cohort_end = 23`（共 24），即持久化链路在启动期间
确实在服务。修之前这个数字是 0，现在是硬失败条件。

**每条记录 CPU 的分母漏了预热请求。** CPU 窗口从预热之前开始、到最后一个计时请求之后
结束，窗口里实际有 `rounds × (warmup + iterations) × 操作数 = 4080` 个请求，而上一版只
除以计时的 3600，把每条成本系统性高估了 13.3%。现在分母由
`_requests_in_window(args, rounds=...)` 统一给出（并有单测固定），输出里也印出这个数字。
运行 A 的 `on-local` 因此从 689 µs 修正为约 608 µs 的量级；本轮 C/D 报的是修正后的
962/953 µs（机器负载不同，不是同一批数字）。

**汇总把两个 cohort 的数据对调了。** 报告曾把 `terminal.stream_attach` 的 on-local
列写进 on-persist 列。现在表格由 JSON 直接生成，列顺序与表头一致，并且本轮起不再手写
任何一行性能数字。

#### 3.3.1 服务端请求（真实 HTTP + 真实 Postgres + 真实 collector/writer）

`bare` 是同应用上未埋点的兄弟路由（写文件 / 终端 resize），不是把埋点从被测路由里删掉。
Δp95 相对同一次运行的 `off`。

| 操作 | 运行 | off p50/p95 | on-persist p50/p95 | on-persist Δp95 | on-local Δp95 | 预算 | 结论 |
| --- | --- | --- | --- | ---: | ---: | ---: | --- |
| files.list.root | C | 3.276 / 10.133 | 3.868 / 8.683 | -1.450 | +1.635 | 10.00 | pass |
| files.list.subdir | C | 3.299 / 9.537 | 3.843 / 8.876 | -0.661 | +3.948 | 10.00 | pass |
| files.open | C | 3.297 / 9.035 | 3.746 / 9.090 | +0.055 | +7.032 | 10.00 | pass |
| terminal.list | C | 4.173 / 12.963 | 4.348 / 12.102 | -0.860 | +3.546 | 10.00 | pass |
| terminal.create | C | 3.890 / 13.550 | 4.550 / 18.719 | +5.169 | -0.126 | 10.00 | pass |
| terminal.stream_attach | C | 3.818 / 11.970 | 3.667 / 10.916 | -1.054 | +0.427 | 10.00 | pass |
| files.list.root | D | 2.684 / 6.477 | 3.263 / 6.545 | +0.067 | +0.585 | 10.00 | pass |
| files.list.subdir | D | 2.844 / 6.109 | 3.578 / 7.647 | +1.538 | +1.119 | 10.00 | pass |
| files.open | D | 2.807 / 5.928 | 3.376 / 7.106 | +1.177 | +4.793 | 10.00 | pass |
| terminal.list | D | 3.296 / 7.359 | 3.468 / 7.106 | -0.253 | +7.450 | 10.00 | pass |
| terminal.create | D | 3.222 / 6.601 | 3.714 / 7.057 | +0.456 | +3.831 | 10.00 | pass |
| terminal.stream_attach | D | 2.911 / 6.400 | 3.568 / 5.926 | -0.474 | +2.418 | 10.00 | pass |

```
  run C: ALL PASS
  run D: ALL PASS
```

六个操作在两次运行里都通过预算。仍然要说清楚的：

- 最接近下限的一格是运行 D 的 `terminal.list` `on-local` +7.450 ms（下限 10 ms），
  同一次运行的 `on-persist` 是 −0.253 ms；运行 C 最接近的是 `files.open`
  `on-local` 约 +7.03 ms，同一次 `on-persist` 是 +0.055 ms。**两次运行里最接近下限的格子
  落在不同的操作上，cohort 都是 `on-local`**，这是"最坏观测仍在下限内"，不是某个操作的稳定成本。
- 同一个 `off` cohort 自己的 p95 在两次之间从 6.6 ms 摆到 13.6 ms（`terminal.create`
  6.601 → 13.550），运行间波动仍大于多数格子的增量，所以逐格的正负号大多不能归因于埋点。
- `bare` 与 `off` 的差同样在噪声内，不能读成"关闭埋点让请求变快"。

#### 3.3.2 四条真实启动路径（真实 Docker 容器 + 真实分类器）

| 路径 | 真实 container_action | resident_action | 运行 | bare | off | on-local | on-persist |
| --- | --- | --- | --- | ---: | ---: | ---: | ---: |
| `new_group` | `create` x24 | `new` | C | 560.0 | 527.7 | 573.2 | 534.4 |
| `resume_stopped` | `start` x24 | `new` | C | 578.8 | 556.2 | 585.1 | 562.4 |
| `resume_running` | `reuse` x24 | `resume` | C | 185.1 | 188.7 | 179.8 | 180.7 |
| `reuse_live` | `reuse` x24 | `reuse` | C | 165.0 | 177.5 | 166.0 | 162.0 |
| `new_group` | `create` x24 | `new` | D | 608.8 | 573.5 | 561.4 | 665.8 |
| `resume_stopped` | `start` x24 | `new` | D | 739.4 | 641.8 | 564.8 | 565.9 |
| `resume_running` | `reuse` x24 | `resume` | D | 183.1 | 182.1 | 186.7 | 182.6 |
| `reuse_live` | `reuse` x24 | `reuse` | D | 169.6 | 176.5 | 167.3 | 173.9 |

数值为 p50（ms）。两次运行的 `recorded_startup_paths` 都是四条路径各 6 条；
`on-persist` 的 hand-off 投递与 cohort 内可见性：

```
   on-persist delivery: {'rows_visible_before_cohort_end': 23, 'delivered': 24, 'dropped': 0, 'drain_timeouts': 0, 'expected': 24}
   on-persist recorded: {'new_group': 6, 'resume_stopped': 6, 'resume_running': 6, 'reuse_live': 6}
   on-persist postgres rows: 24
   on-persist delivery: {'rows_visible_before_cohort_end': 23, 'delivered': 24, 'dropped': 0, 'drain_timeouts': 0, 'expected': 24}
   on-persist recorded: {'new_group': 6, 'resume_stopped': 6, 'resume_running': 6, 'reuse_live': 6}
   on-persist postgres rows: 24
```

**这个 n 说明不了尾部**：n=6 时 nearest-rank 的 p95 就是最大值，脚本自己在输出里写明。
四条路径的 on/off p50 差值在两次之间方向不一致（运行 D 的 `new_group` on-persist 比 off
高 92 ms，运行 C 同格低 7 ms），而且与真实容器动作的数百毫秒同一量级，**没有一条能读成
启动路径的采集开销**。真实可用的结论是：埋点成本落在容器动作噪声之下，且窗口确实覆盖了
它——后者由 §3.3.0 的注入实验与常驻回归测试证明，不由上表证明。

**这些不是生产数字**：colima/Lima + runc，无 Kata；边界止于 supervisor acquire +
resident 交接，resident CLI 是替身传输层。

#### 3.3.3 丢弃与整进程 CPU

正常负载下零丢弃：

```
run C on-persist drops: {'stored': 4080, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 0, 'dropped_timeout': 0, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'stored': 4080}} postgres rows: 4080
run D on-persist drops: {'stored': 4080, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 0, 'dropped_timeout': 0, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'stored': 4080}} postgres rows: 4080
```

整进程 CPU（`time.process_time()`，含请求路径、collector exporter、控制面 writer）：

| 运行 | cohort | 整进程 CPU | 相对 off | 每条记录 | 峰值 RSS |
| --- | --- | ---: | ---: | ---: | ---: |
| C | bare | 14.731 s | — | n/a | 249.4 MiB |
| C | off | 10.377 s | — | n/a | 249.4 MiB |
| C | on-local | 14.302 s | +3.925 | +962.0 | 249.4 MiB |
| C | on-persist | 13.619 s | +3.242 | +794.6 | 249.3 MiB |
| D | bare | 12.240 s | — | n/a | 175.3 MiB |
| D | off | 8.290 s | — | n/a | 177.5 MiB |
| D | on-local | 12.178 s | +3.888 | +952.9 | 182.9 MiB |
| D | on-persist | 11.520 s | +3.230 | +791.6 | 172.4 MiB |

```
-- run C: 4080 requests in the window, 4 rounds
-- run D: 4080 requests in the window, 4 rounds
```

即"开启后整个进程在同一批请求上多花 3.2–3.9 s CPU，约 790–960 µs/条（分母 4080）"。
这**不是**后台线程的独立测量：进程内同时跑着客户端与服务端，客户端自身也因多出的记录而
多做工作，本平台又无法按线程拆分，因此只能读成"该配置带来的整进程额外 CPU"。峰值 RSS
两次都没有可归因的增长。

#### 3.3.4 故意变慢

```
-- run C slow_sink: p95 6.4-12.5 ms
     local_destination: {'path': '/var/folders/zp/ntcw2tq10fsc73h4sj8mh5v40000gn/T/operation-overhead-bench-vxfs59js/local-slow-sink-r0.jsonl', 'records_expected': 1020, 'records_written': 662, 'records_dropped': 358, 'bytes': 1811666, 'bytes_per_record': 2736.7, 'synthetic_per_record_delay_ms': 20.0}
-- run C slow_db: p95 4.2-9.9 ms
     requests_answered: 360
     writer_at_end_of_hold: {'stored': 0, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 2, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104, 'write_timeout': 2}}
     recovered_after_s: 0.553
-- run D slow_sink: p95 10.0-25.5 ms
     local_destination: {'path': '/var/folders/zp/ntcw2tq10fsc73h4sj8mh5v40000gn/T/operation-overhead-bench-lko5ax3a/local-slow-sink-r0.jsonl', 'records_expected': 1020, 'records_written': 733, 'records_dropped': 287, 'bytes': 2005327, 'bytes_per_record': 2735.8, 'synthetic_per_record_delay_ms': 20.0}
-- run D slow_db: p95 6.6-31.5 ms
     requests_answered: 360
     writer_at_end_of_hold: {'stored': 0, 'duplicate': 0, 'rejected': 0, 'conflict': 0, 'dropped_full': 104, 'dropped_timeout': 2, 'dropped_shutdown': 0, 'failed': 0, 'reasons': {'queue_count': 104, 'write_timeout': 2}}
     recovered_after_s: 0.274
```

两个 arm 都是**合成**的，脚本标注了合成在哪一层（`slow_sink` 是 bench-local 目的地，
不是控制面 ingest；`slow_db` 是 PostgreSQL，由脚本从第二条连接持锁）。两次运行都是
360 个请求全部应答、最多丢弃并计数、释放后在 22 s 预算内恢复。

#### 3.3.5 这次实验**没有**证明的事

- 不是端到端延迟：浏览器、网络、host agent 与沙箱都不在任何数字里，没有跑 agent CLI
  或模型。
- 服务端请求的 broker 是 in-process 替身，只讲真实片段协议；文件/终端请求背后没有
  容器工作。
- 服务端操作的 `bare` 是同应用上未埋点的兄弟路由，不是把被测路由的埋点删掉。
- `on-local` 用 bench-local JSONL 替身，因为部署里没有"去掉 Postgres 仓储"的开关。
- 启动路径 n=6，p95 即最大值；逐路径开销没有测出来。
- 整进程 CPU 差值无法按线程归因，也包含客户端自身多出的工作。
- 请求速率是持续每几毫秒一次，远高于人类浏览；每轮之间会等有界队列排空。
- 未发送浏览器的 `X-Agent-Interaction-Id` 续接。
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
