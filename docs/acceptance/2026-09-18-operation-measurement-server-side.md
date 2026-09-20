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
| 快速层 | `pytest -q -m "not external and not dbtest"` | 4696 passed, 595 deselected |
| 数据库层 | `TEST_DB_NAME=ca_startup_measurement_test_db pytest -q -m dbtest` | 543 passed, 4748 deselected |
| 格式 | `ruff format --check .` | 473 files already formatted |
| Lint | `ruff check --no-fix .` | All checks passed |
| 类型 | `mypy` | Success: no issues found in 218 source files |
| Web 单测 | `vitest run` | 102 files, 1637 tests passed |
| Web 生产构建 | `next build` | 成功，路由表含 `/admin/measurements` 与 `/admin/runs/[jobId]` |
| OpenAPI 契约 | `pytest tests/unit/test_api_surface_contract.py` | 4 passed（契约已按新增两个查询参数重新生成） |

数据库层使用独立测试库 `ca_startup_measurement_test_db`（名称含 `_test_`，fixture 会
拒绝其他名字），跑完自动 truncate 自己创建的行。真实链路验收另用
`ca_acceptance_measure_test_db`。

一次并发跑两个 DB 层级会话时，
`test_legacy_database_copy.py::test_cross_owner_row_rolls_back_archive_and_entire_schema`
失败一次；单独重跑该文件 5 passed，串行重跑整个层级 543 passed。这是两个会话同时动数据库
造成的互相干扰，不是本轮改动；记在这里以免下次把它当成回归。

### 3.3 真实业务开销实验

脚本：`scripts/bench_operation_overhead.py`。第 2 次运行的完整表格留档在
[run2-overhead.md](assets/2026-09-18-operation-measurement/run2-overhead.md)；两次运行的
机器可读 JSON 由脚本 `--json` 产出（未入库，因为单次约 0.5 MiB，脚本可重跑复现）。
环境：python 3.13.9、macOS 15.7.7 arm64、14 CPU、PostgreSQL 15.17、Docker 28.4.0
（colima/Lima VM，runc）。固定单进程、单 replica、串行 cohort、每 cohort 预热 20、
每操作 4 轮 × 150 = **n=600**。百分位用 nearest-rank（Admin 页面用 SQL
`percentile_cont`，会插值，两者数字不会逐位相同——脚本自己写明）。预算取设计约定的
`max(10 ms, off_p95 × 1%)`，baseline 为 `off`。全部数字来自
`scripts/bench_operation_overhead.py` 的真实运行，没有一处是估算或模拟。

#### 3.3.1 服务端请求（真实 HTTP + 真实 Postgres + 真实 collector/writer）

单位 ms。`bare` 是同一应用上**未埋点**的兄弟路由（写文件 / 终端 resize），不是把埋点从
被测路由里删掉——CP 路由永远经过 collector 接缝，脚本不改生产代码，所以这一点在下面
的 caveat 里明确写出。

**同一配置连跑两次**，因为一次运行的 p95 在共享机器上不足以下结论。两次都是
n=600/操作（4 轮 × 150），预算 `max(10 ms, off_p95 × 1%)`：

| 操作 | cohort | off p50/p95（第 1 次） | on-persist p50/p95（第 1 次） | Δp95 | off p50/p95（第 2 次） | on-persist p50/p95（第 2 次） | Δp95 | 结论 |
| --- | --- | --- | --- | ---: | --- | --- | ---: | --- |
| files.list.root | off / on-persist | 2.652 / 3.984 | 2.855 / 3.937 | −0.046 | 3.423 / 11.532 | 3.596 / 7.662 | −3.870 | 两次 pass |
| files.list.subdir | 同上 | 2.739 / 5.898 | 2.821 / 3.813 | −2.085 | 3.499 / 10.385 | 3.363 / 8.010 | −2.375 | 两次 pass |
| files.open | 同上 | 2.800 / 6.438 | 3.116 / 4.903 | −1.535 | 3.448 / 13.427 | 3.728 / 7.110 | −6.317 | 两次 pass |
| terminal.list | 同上 | 2.989 / 4.568 | 3.259 / 4.577 | +0.008 | 4.015 / 13.290 | 3.965 / 10.170 | −3.120 | 两次 pass |
| terminal.create | 同上 | 3.020 / 5.164 | 3.417 / 5.385 | +0.221 | 3.947 / 11.738 | 4.065 / 10.418 | −1.319 | 两次 pass |
| terminal.stream_attach | 同上 | 2.857 / 5.960 | 3.173 / 4.628 | −1.331 | 3.616 / 12.531 | 4.018 / 11.169 | −1.362 | 两次 pass |

**六个操作在两次运行中都通过预算**，绝对增量都远低于 10 ms 下限。但结论只能到这里，
不能再细：

- **运行间波动大于被测增量。** 同一个 `off` cohort 自己的 p95 在两次之间就差了 2.9 倍
  （`files.list.root` 3.98 → 11.53 ms），第二次机器明显更忙。任何小于这个摆动的逐操作
  Δp95 都不能归因于埋点。
- 第一次运行里 `terminal.list` 的 `on-local` 曾出现 p95 +3.46 ms（超出该次轮间噪声底
  0.50 ms，且四轮 p50 方向一致），第二次同一格是 −3.13 ms。**该增量未能复现**，因此
  记为运行间波动，而不是一条稳定的采集成本，也不写成“已定位到 JSONL 替身”。
- 多数 Δp95 为负同样只是噪声，不能读成“采集让请求变快”。

#### 3.3.2 四条真实启动路径（真实 Docker 容器 + 真实分类器）

同一脚本的 `--startup` 段真实 create/start/reuse 容器，并把真实 collector 包在真实
supervisor activation 外。`container_action` 计数证明四条路径都真的产生（这是
`new_group` 修复后的第一次端到端证据）。两次运行分别 n=5 与 n=6/cohort/path，每轮
每路径 4 个 cohort：

| 路径 | 真实 container_action | 真实 resident_action | 第 1 次 off p50 → on-persist p50 | 第 2 次 off p50 → on-persist p50 |
| --- | --- | --- | ---: | ---: |
| `new_group` | `create` ×20 / ×24 | `new` | 614.4 → 634.6 | 580.8 → 790.8 |
| `resume_stopped` | `start` ×20 / ×24 | `new` | 542.8 → 546.6 | 569.7 → 564.1 |
| `resume_running` | `reuse` ×20 / ×24 | `resume` | 183.2 → 181.3 | 188.2 → 180.2 |
| `reuse_live` | `reuse` ×20 / ×24 | `reuse` | 174.8 → 167.5 | 168.9 → 172.1 |

`create`/`start` 计数为零以外的值才说明路径真的走到；四个路径在两次运行里都由正确的
真实动作产生。真实容器 create/start 是数百毫秒级（`new_group`/`resume_stopped` 的 p50
在 0.54–0.79 s），`reuse` 路径在 0.17–0.19 s；on-persist 与 off 的 p50 差在这些量级下
方向不一致（第 2 次 `new_group` 的 +210 ms 来自 6 个样本里的单个 1.53 s 最大值，
nearest-rank 下 p95 就等于最大值）。**这两次都不能给出启动路径的尾部结论**：n=5/6 时
p95 是最大值而不是尾部估计，脚本自己在输出里写明并提示加大 `--startup-iterations`。
**这些数字不是生产数字**：本机是 colima/Lima + runc，无 Kata，边界是
“supervisor acquire + resident 交接”，resident CLI 是替身传输层，所以 CLI 自身初始化不在
其中。

#### 3.3.3 丢弃与后台成本

| cohort | sink 写入/预期 | writer stored | dropped_full | dropped_timeout | rejected | failed |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| bare / off | 0/0 | 0 | 0 | 0 | 0 | 0 |
| on-local | 4080/4080 | 4080 | 0 | 0 | 0 | 0 |
| on-persist | 4080/4080 | 4080 | 0 | 0 | 0 | 0 |

正常负载下零丢弃（两次运行都是 4080/4080 写入、0 丢弃）。后台成本（4 轮累计，两次
运行）：非请求线程 CPU `bare 1.35/1.62s`、`off 0.91/1.13s`、`on-local 1.47/1.71s`、
`on-persist 1.77/1.95s`；因此 4,080 条记录的后台线程成本约 **0.6–0.8 s CPU**，均摊到
每条记录约 150–200 µs，且不在请求路径上。峰值 RSS 第 1 次 off 158.9 → on-local
161.4 MiB（+2.5 MiB），第 2 次两次采样相同（169.2 MiB），所以 RSS 结论只到“没有可测的
增长”。脚本明确列出**没测到**的东西：网络字节（loopback 无便携的 per-socket 计数器）、
可分线程 CPU（本平台不可分）、WAL/索引/磁盘字节、容器 CPU/内存。

#### 3.3.4 故意变慢

两次运行都跑了两个合成 arm，结果一致：

| 场景 | 合成方式 | 业务请求 | 结果 |
| --- | --- | --- | --- |
| 慢目的地 | bench-local 落盘每条延迟 20 ms，跨操作累积 | 6 个操作 p95 4.6–7.3 ms（第 1 次）/ 9.6–18.1 ms（第 2 次，机器更忙） | `dropped_full=382`（第 1 次）/ `198`（第 2 次），reason 均为 `queue_count`；业务请求不受影响 |
| 慢数据库 | 第二条连接对 `agent_operation_measurements` 持 `ACCESS EXCLUSIVE` | 锁定期间 **360 个请求全部应答**（两次相同），p95 4.0–7.0 / 4.6–17.6 ms | `dropped_full=104`、`dropped_timeout=2`（`write_timeout`）；释放后 0.271 s / 2.902 s 内恢复（预算 22.0 s），254 行落库 |

两个 arm 都是**合成**的，脚本在报告里标注了合成的是哪一层（`slow_sink` 是 bench-local
目的地，不是控制面 ingest；`slow_db` 是 PostgreSQL，由脚本从第二条连接持锁）。它们的
价值在于证明“诊断拥塞只丢样本、不改业务结果”这条设计承诺，而不是给出生产数字。

#### 3.3.5 这次实验**没有**证明的事

脚本的 `not_what` 与 `caveats` 逐条列出，其中对本 PR 结论最要紧的是：

- 不是端到端延迟：浏览器、网络、host agent 与沙箱都不在任何数字里，没有跑 agent CLI
  或模型。
- 服务端请求的 broker 是 in-process 替身，只讲真实片段协议；文件/终端请求背后没有
  容器工作。
- 服务端操作的 `bare` 是同应用上未埋点的兄弟路由，不是把被测路由的埋点删掉。
- `on-local` 用 bench-local JSONL 替身，因为部署里没有“去掉 Postgres 仓储”的开关。
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
