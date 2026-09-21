# Admin 按需测量：开始、停止和运行时生效 —— 实现与验收

日期：2026-09-21。被测分支：`murphy/deepseek/admin-measurement-runtime-control`，目标 `dev`，
基线 `2aa35f4`（PR #258 合并提交）。设计文档：
`docs/specs/2026-09-21-admin-measurement-runtime-control-design.zh-CN.md`。

本文只记录**实际跑过**的检查。凡是没跑过的写“未覆盖”并说明缺什么；凡是用替身代替真实
组件的写“模拟”，不与实测混列。

## 1. 交付范围

管理员在 **Admin → Measurements** 页面点击 **开始测量 / 停止测量**，即可控制沙盒启动、
终端打开、文件访问三类服务端采集的开启与停止，**不需要改环境变量、不需要重新部署、不需要
重启任何服务**，到期自动停止。

| 层 | 落点 |
| --- | --- |
| 采集端策略源 | `apps/backend/cloud_agent_profiling/operation_profiling.py`：`AGENT_OPERATION_PROFILING_MODE`、`ControlState`、`RunAuthorization`、`RuntimePolicyStore`、可安装的 `PolicyProvider` |
| 控制能力 | `apps/backend/cloud_agent/ports/profiling_control.py`、`domain/profiling_control.py`、`storage/repositories/profiling_control.py`、`application/profiling_control.py` |
| 持久化 | `migrations/versions/0018_profiling_control.py`：控制单例行 + 命令幂等表 + 参与者状态表 + run 生命周期列 |
| Admin 接口 | `apps/backend/cloud_agent/api/admin_profiling_control.py` |
| 配置传播 | `apps/backend/cloud_agent/workers/profiling_control.py`（CP 每进程快照）、`apps/backend/cloud_agent/api/host_profiling.py`（host 策略交换）、`apps/backend/cloud_agent_host/profiling_policy.py`（runner 同步 + broker 下发）、`apps/backend/cloud_agent_host/broker_profiling_policy.py`（broker 接收） |
| 前端 | `apps/frontend/src/app/(agents)/admin/measurements/MeasurementControlPanel.tsx`、`src/lib/api/adminMeasurements.ts` |
| 部署 | `deploy/{control-plane,host,local}` 三个 compose + 两个 `.env.example` + `ENVIRONMENT.md` |

## 2. 使用说明

### 2.1 部署侧一次性开启（每个环境一次，之后不再需要）

```bash
# 三个采集进程都要有：CP、runner、workspace broker（两个容器）
AGENT_OPERATION_PROFILING_MODE=admin

# 只有共享同一个数据库的多个部署需要设置；单部署留空即 default
AGENT_OPERATION_PROFILING_DEPLOYMENT=staging
```

其余 `AGENT_OPERATION_PROFILING_ENABLED / _RUN / _SINK` 保持原样即可，`admin` 模式下
它们**不再决定**是否采集：旧的 `RUN` 不会在 Admin 停止后被当作 fallback，旧的
`ENABLED=false` 也不会否决管理员的开始。迁移用 `alembic upgrade head`（新增
`0018_profiling_control`）。

`MODE` 是"这个部署允许用什么方式控制"，不是每次实验的开关；改它需要重启，**此后每次
开始/停止都不需要**。

### 2.2 管理员操作

1. 打开 **Admin → Measurements**，页面顶部出现 `Measurement collection` 卡片。
2. 点 **Start measurement**，选择操作（默认全选三项）、目标（一台 host 或 All hosts，
   必须显式选择）、时长（1 / 5 / 10 / 30 分钟，默认 10），提交。
3. 卡片显示 `Collecting` / `Partly active`，以及 `Host:`、`Time left: mm:ss`、
   `Stored samples: N`。期间正常使用产品即可，**测量不会主动启动沙盒、打开终端、读取
   文件或运行模型**；没有匹配操作时显示的是"尚无匹配操作"而不是 0 ms。
4. 点 **Stop measurement** 立即停止采集；不弹确认框，不中断任何用户操作，不删除数据。
   已入库样本、历史和筛选全部保留。
5. 到期（或 CP 重启、页面关闭）都会自动停止：期限是权威时间，不依赖页面打开或清扫执行。

### 2.3 时间与状态口径

| 状态 | 判据 |
| --- | --- |
| `Off` | 没有活动实验（`run` 为空，且没有已结束的实验可报告） |
| `Starting` | 已持久化，所选范围内还没有进程确认当前 revision |
| `Collecting` | 所选范围内的预期参与者都确认了当前 revision |
| `Partly active` | 有参与者未确认/不支持/落后/本地额度耗尽；`gaps[]` 逐条说明 |
| `Stopped` / `Expired` | 结束原因来自 `last_stopped_run.stop_reason`；仍可开始新实验 |
| `Unavailable` / `Managed by deployment configuration` | 部署未开 Admin 控制（`mode` 不是 `admin`）或没有控制行；**不伪装成 Off** |

传播时限（实现值，见第 5 节）：健康节点上开始最迟 35 s 内生效；每个授权有效期
**15 s**，停止后所有旧授权最迟 **20 s** 内失效（含断网节点）。

## 3. 实测通过（真实组件）

以下每一项都在本机真实运行，命令与结果如实抄录。

### 3.1 仓库质量门

```bash
.venv/bin/python -m ruff format --check .        # 486 files already formatted
.venv/bin/python -m ruff check --no-fix .        # All checks passed!
.venv/bin/python -m mypy                         # Success: no issues found in 228 source files
.venv/bin/python -m pydocstyle <四个包>          # 无输出
PYTHONPATH=apps/backend:. pytest tests/unit tests/servers -q   # 见 3.2
cd apps/frontend && npm run lint && npm run format:check && npm run type-check && npm run test
```

前端四项 gate 结果：`eslint --max-warnings 0` 无告警；Prettier 全部符合；
`tsc --noEmit` 无错误；`vitest run` **103 个文件 / 1673 个用例全部通过**。

### 3.2 全量测试

| 命令 | 结果 |
| --- | --- |
| `pytest tests/unit tests/servers -q` | **4806 passed, 1 failed**（唯一失败见 §6.1，本机 `os.openpty` 被拒，与本 PR 无关） |
| `pytest -q -m dbtest`（真实 PostgreSQL 16 容器，Alembic 迁移到 head） | **548 passed** |
| `pytest -q -m "not external and not dbtest"`（`make test` 的等价集合） | 同上第一条 |

### 3.3 真实 HTTP + PostgreSQL + 独立 broker 进程链路

`tests/integration/servers/test_profiling_control_acceptance_db.py`（新增，5 个用例，
连跑 3 次结果一致）。它做的事：

1. 真实 Alembic 迁移建表，真实 `asyncpg` 连接池，真实 FastAPI 应用跑在真实 uvicorn
   socket 上（不是 ASGI 直调）。
2. `POST /admin/agent/profiling-runs` → PostgreSQL 里创建实验与控制行 → CP 自己的策略
   循环把快照发布到采集器；断言 `published.run.source == "admin"`、
   `authorization_ttl_s <= 15`、`authorization.deadline_mono < run.deadline_mono`
   （即授权确实短于实验期限，断网会先停）。
3. 用**真实属主会话 cookie** 调 `GET /v1/agent/jobs/{job}/files`，经真实
   `operation_measurement` 选根 → 真实 broker 片段协议 → 有界投递 → PostgreSQL；断言
   落库行的 `profiling_run_id`、`producer`、`record.run_source == "admin"`、
   `record.control_revision == 1`。
4. `GET /admin/agent/profiling-control` 读回：`stored_samples == 1`、
   `seconds_remaining > 0`；`GET /admin/agent/measurements?run_id=...` 能读到该样本
   （即“保留历史”）。
5. 同一 `request_id` 与同一 body 重试 → **同一个 run、同一期限**（幂等）。
6. `POST .../{run_id}/stop` → `revision` 递增、`run_active=false`、
   `last_stopped_run.run_id == run_id`、`status == stopped`；换一个 `request_id` 再停
   一次 → 仍是同一 revision（重复停止无副作用）。
7. 停止后再读同一个文件 → 响应里**没有** `profiling` 块，且该 run 的入库行数仍为 1
   （停止不删数据，也不新增样本）。

另外 4 个用例覆盖：第二个 Start 被 409 `already_active` 拒绝并带回当前 revision；
非 Admin 模式返回 409 `not_admin_mode` 且状态是 `unmanaged`；未知操作/未知 host/超时长/
未知字段在**不移动控制行**的前提下被 422 拒绝；匿名调用读写都是 401/403。

### 3.4 真实 HTTP 的接口与策略交换

`tests/servers/test_profiling_control_api.py`（新增，15 个用例）用真实路由、真实依赖、
真实请求/响应模型跑通：

- Start 的形状、审计写入、幂等重放、`already_active` / 陈旧 revision 的 409 语义。
- Stop 的幂等、`last_stopped_run`、以及**旧页面不能停掉新实验**（run id 校验）。
- 部署没有控制行 → `unavailable`（不是 `off`）；`environment` 模式 → `unmanaged` 且写入
  409 `not_admin_mode`。
- host 策略交换：host 上报自己的 `applied_revision` 与 broker 确认 → 回答里带着当前
  run、`lease.seconds ∈ (0, 15]`、`broker_confirmed_revision`；不支持能力的 host 被记成
  `unsupported` 而不是"没回答"。
- 租约随请求在途时间收缩：把同一个回答的 `server_time` 往后挪 12 s，
  `lease_deadline_mono` 相应少 12 s（约剩 3 s）——即延迟到达的回复买不到它没有的时间。
- 已停止的部署给 host 的回答里 `run` 为 null（是"停止"命令，不是"没有消息"）。
- broker 端点：同 revision 幂等、**更低的 revision 被 409 拒绝**（乱序重放无法撤销
  Stop）、没有策略时 `start_stage_recording` 返回 `None`。

### 3.5 采集器运行时语义（单元）

`tests/unit/test_operation_profiling_runtime_control.py`（新增 23 个用例）覆盖模式解析
（未设 → 旧语义；`disabled`；`environment`；`admin` 忽略旧 `ENABLED/RUN`；拼错的值
会告警并关闭）、Stop 到达已打开测量、续租不重置预算、切换实验不串数据
（旧测量仍用旧 run 的 manifest/target/`run_id`）、有界租约、`budget_state` 口径、
以及 `handoff_context` → `attach_selection` 的父子继承契约。

`tests/unit/test_profiling_control_application.py`（新增 70 个用例）覆盖服务层的每一个
拒绝原因、每一个 `effective_status` 分支（含"截止时间优先于生命周期列"与"没有任何预期
参与者时是 starting 而不是 collecting"）、快照口径与 sweep 语义。

### 3.6 父子进程继承与停止传播（真实 runner 进程）

`tests/unit/test_runner_host_service.py`、`tests/unit/test_agent_runner.py`、
`tests/unit/test_shared_session_supervisor.py`、`tests/unit/test_agent_workspace_broker.py`
全部保持通过（325 个用例）。这些用例里 runner 父进程真实 `fork/exec`
`python -m cloud_agent_host.runner --turn`，`_profiling` 经 JSON handover 传给子进程，
子进程 `attach_selection` 重建**同一个 measurement_id 与起点**；新增的授权与
manifest 字段随 handover 走，子进程不再重新读自己的策略快照做判定。

### 3.7 部署配置

`tests/unit/deploy/test_artifact_compose.py` 与 `tests/unit/test_deploy_config.py`
已扩展并保持通过：三个 compose 文件都把 `AGENT_OPERATION_PROFILING_MODE` 传给 CP、
runner、workspace broker，本地栈的 deployment 命名空间也按 `${...:-default}` 透传。

## 4. 模拟通过（替身代替了真实组件）

| 用例 | 真实部分 | 替身部分 | 为什么 |
| --- | --- | --- | --- |
| 3.3 的 broker 片段 | 真实 `operation_call.open_call` / `BrokerCall` 校验、真实 runner↔broker HTTP 片段协议、真实投递与入库 | 工作区内容（`README.md` 的字节）由固定 fixture 提供；没有真实容器 | 本机没有可用的容器运行时 |
| 3.4 的存储 | 真实路由/依赖/模型/传输 | 内存版 control capability | 该文件测的是 HTTP 语义，SQL 语义由 3.3 的真实 PostgreSQL 覆盖 |
| 3.4 的 broker 端点 | 真实 `create_app`、真实共享令牌校验、真实策略存储 | `ProcessBackend` + 空 supervisor | 策略路由不碰容器 |

**没有**把 mock 延迟当生产实测：第 5 节的时限来自代码常数与真实 HTTP 往返，
不是压测结果。

## 5. 时限口径（实现事实，非实测延迟）

| 环节 | 值 | 位置 |
| --- | --- | --- |
| CP 快照轮询（空闲 / 活动） | 30 s / 5 s | `workers/profiling_control.py` |
| host 策略同步（空闲 / 活动 / 失败重试） | 30 s / 5 s / 10 s | `cloud_agent_host/profiling_policy.py` |
| 单次策略请求超时 | 2 s | 同上 |
| 授权有效期 | 15 s | 同上 + `api/host_profiling.py` |
| 停止传播上限（新 Start 的等待窗口） | 20 s | `storage/repositories/profiling_control.py` |
| 开始生效上限 | 30 s（心跳间隔）+ 一个同步周期 | 设计契约 35 s |

结论：健康环境 Start 在 **35 s 内**生效（UI 期间显示 `Starting`）；Stop 后 **20 s 内**
所有旧授权失效；断网节点在最后一次成功同步 + 15 s 后自行停止。三者都由本地单调时钟
执行，不比较不同机器的 monotonic 值。

## 6. 未覆盖 / 环境限制

1. **`tests/unit/test_chat_sandbox.py::test_retained_terminal_keeps_the_shell_in_the_docker_tty_process_group`
   在本机失败**，原因是 `os.openpty()` 被本机沙箱拒绝
   （`PermissionError: [Errno 1] Operation not permitted`），与本 PR 无关：该文件未被
   本 PR 修改，失败发生在容器终端建立阶段。CI 上应通过。
2. **没有真实 Docker/Kata 容器参与验收**。因此"沙盒启动"（`attempt.startup`）的
   broker 子阶段（container create/start、CLI 启动）在本文档里没有实测证据，只有单元
   与路由级证据。需要一台能跑容器的 host 才能补。
3. **没有跨 CP replica 的真实部署验证**。同一数据库两个进程同时 Start 的"只有一个成功"
   由控制行的 `FOR UPDATE` + 事务级 advisory lock 保证，并有真实 PostgreSQL 的
   409 路径覆盖（3.3 第 2 个用例是同一 replica 的顺序请求），但**没有**两个真实 replica
   并发的实测。
4. **没有真实断网（网络分区）实测**。租约到期由 3.4 的"把 `server_time` 推后 12 s"和
   3.5 的短租约单测覆盖，属于受控时钟验证，不是拔网线验证。
5. **浏览器截图未附**。前端由 `vitest`（jsdom，103 文件 / 1673 用例）覆盖，但没有在真实
   浏览器里点过 Start/Stop 并截图；按设计文档 §8 的最后一行，这属于应在有浏览器环境的
   验收中补做的项。
6. **FI staging 配置未改**。见 `docs/acceptance/2026-09-21-admin-measurement-runtime-control-fi.md`。
7. **没有做性能/压力实测**。设计文档 §7 要求"基准记录关闭时额外后台成本与开启开销"，
   本轮只给出了轮询频率与单次开销的量级，没有跑 `scripts/bench_*`。

## 7. 已知取舍（写下来供评审）

1. **停止原因分三档**：`expired`（实验到期）、`stopped`（管理员停止）、
   `stopped_locally`（本进程的授权被撤销或租约到期）。第三档让"控制面失联导致的停止"
   与"管理员明确停止"在记录里可区分，但**不能**告诉读者管理员是否点过 Stop；那需要
   把 tombstone 的 revision 也写进在途记录，本轮没做。
2. **broker 只接受比它已应用更新的 revision**，相同 revision 幂等、更低的是 409。
   代价是：如果 runner 因为重启把 revision 回退（实际不会发生，revision 只在数据库里
   单调递增），broker 会拒绝直到重新对齐。
3. **`All hosts` 的目标在 API 层是空 target**，因此"新加入的 host"也会被算进预期参与者
   （它确实会被要求采集）。这是设计意图，但意味着一个长期运行的全站实验在加机器后会
   短暂显示 `Partly active`。
4. **`agent_operation_profiling_control` 多了一行 `deployment = ''` 的兜底行**。命名了
   部署却没有该行时，读写都落到这一行；第一次 Start 会写入部署自己的行。这让"改了
   部署名"不会退化成"按钮没反应"，代价是多一行哨兵数据。
5. **每个 root 生产进程的额度仍是 100（部署级硬上限 1000）**，没有全站精确上限。API
   返回 `budget_scope=producer_instance`，页面文案也这么写。

## 8. 证据摘要

本轮实跑的命令与结果（完整输出粘贴在 PR 描述里）：

```
$ .venv/bin/python -m ruff format --check .
486 files already formatted
$ .venv/bin/python -m ruff check --no-fix .
All checks passed!
$ .venv/bin/python -m mypy
Success: no issues found in 228 source files
$ .venv/bin/python -m pydocstyle apps/backend/cloud_agent apps/backend/cloud_agent_host \
      apps/backend/cloud_agent_profiling apps/backend/cloud_agent_shared
(无输出)

$ PYTHONPATH=apps/backend:. pytest tests/unit tests/servers -q -p no:cacheprovider --timeout=300
1 failed, 4806 passed, 5 warnings in 301.96s
  └─ FAILED tests/unit/test_chat_sandbox.py::test_retained_terminal_keeps_the_shell_in_the_docker_tty_process_group
     PermissionError: [Errno 1] Operation not permitted  (os.openpty，见 §6.1)

$ PYTHONPATH=apps/backend:. TEST_DB_PORT=55432 pytest -q -m dbtest -p no:cacheprovider --timeout=300
548 passed, 4875 deselected, 696 warnings in 449.10s

$ PYTHONPATH=apps/backend:. TEST_DB_PORT=55432 pytest \
      tests/integration/servers/test_profiling_control_acceptance_db.py -q -m dbtest
5 passed   （连跑 3 次，结果一致）

$ PYTHONPATH=apps/backend:. pytest tests/servers/test_profiling_control_api.py -q
15 passed

$ PYTHONPATH=apps/backend:. pytest tests/unit/test_profiling_control_application.py \
      tests/unit/test_operation_profiling_runtime_control.py \
      tests/unit/test_agent_operation_profiling.py -q
173 passed

$ cd apps/frontend
$ npm run lint          # 无告警（--max-warnings 0）
$ npm run format:check  # All matched files use Prettier code style!
$ npm run type-check    # 无错误
$ npm run test          # Test Files 103 passed (103) / Tests 1673 passed (1673)
```

未跑：`scripts/bench_startup_profiling_overhead.py`、`scripts/bench_operation_overhead.py`
（本轮未做性能基准，见 §6.7）；`pytest -m external`（需要外部服务）。
