# Agent Operation 可观测性与资源归属设计

**日期：** 2026-09-01

**修订：** 2026-09-03，对齐 [#89](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/89) / [#102](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/102) 落地的 Kata guest eBPF

**状态：** 分层设计。Attempt 级 Docker Engine 采样、Claude Code exact hook 边界、Kata guest-root CO-RE 进程普查与 exit-time CPU/RSS/I/O 已落地。通用 operation ledger、host-side BPF、网络/文件 eBPF 仍是提案。Staging Kata/KVM 验收尚未关闭。

**实现仓库：** `freeinference-cloud-agent`；Inference cost truth 位于 `hybridInference`

**相关文档：**

- [Agent 会话连续性与共享沙盒架构](2026-09-01-agent-session-continuity-architecture.zh.md)
- [Agent 沙盒安全模型与能力边界](2026-09-01-agent-sandbox-security-model.zh.md)
- [Observability 与资源核算规范](2026-08-19-observability-and-resource-accounting-design.md)（实现契约；已含 guest observer）
- [Native session fork 与 shared workspace 设计](2026-08-31-native-session-fork-shared-workspace-design.md)

## 1. 实现状态

本文首先区分当前实现与后续设计，避免把研究方向写成已交付能力。

| 能力 | 状态 | 当前 truth |
|---|---|---|
| Canonical Agent event ledger | 已实现 | `agent_job_events`，按 `(attempt_id, client_seq)` 幂等 |
| Attempt 资源采样 | 已实现 | Host 通过 Docker Engine stats 采样，写 `agent_attempt_resources` |
| Measurement coverage | 已实现 | `coverage` + nullable counter；不可读不伪造为零 |
| Gateway token/cost ledger attribution | Gateway 已实现；Cloud Agent 读取未接通 | Gateway 写 `api_logs.agent_job_id` 且提供 grant usage contract；当前 `GrantClient` 没有 usage read，job response 不返回 join 后 projection |
| Shared sandbox per-member periodic CPU/memory/I/O | 未实现 | Kata 下 hypervisor/container total 不能诚实分给 member；明确 `coverage=0`，禁止复制 group total |
| Per-tool timing window | 已实现（按 runtime capability） | Claude Code 用平台 PreToolUse/PostToolUse hook 得 `exact`；Codex/无 hook Claude/Pi 为 `message`；Kilo/OpenCode 为 `none`；overlap 记 `shared_*` |
| Per-tool process census | 已实现（Kata） | Guest-root CO-RE observer 记录 process-leader `fork`/`exec`/`exit`；线程折入 leader；marker helper 不计 workload |
| Per-tool exit-time CPU/RSS/I/O | 已实现（Kata） | `sched_process_exit` 取 peak RSS 与 I/O；`sched_switch` 在 `TASK_DEAD` 取 `se.sum_exec_runtime`；缺 BTF 字段报 absent，不报 0 |
| Egress flow parsing 与 unclassified byte ledger | 未实现 | 生产 `platform_only` 没有可解析的 CONNECT 日志；不是当前 eBPF 范围 |
| General operation ledger | 本文提案 | 需要统一 turn/tool/terminal/file/Git/Fork/snapshot identity |
| Host-side OperationSensor / 网络与文件 eBPF | 本文提案 | 未被采用。生产路径是 guest observer，不是 host BPF daemon |
| Admin aggregate、alert checker、OTLP export | 延后 | 不属于当前 per-chat 首发范围 |

因此，当前可以回答：

- 某个 attempt 的 container-level 资源大约是多少（Docker stats；Kata 下 periodic 覆盖常为不可用）；
- 某个 Kata turn 在 cursor 之后新出现了哪些进程，以及其中已结算进程的 CPU、peak RSS 与 I/O；
- 某个只归属一个 tool window、且 thread group 已结算的调用，其子进程消耗了多少（floor；lossy/overlap/未结算则标 `process_resources_partial`）。

仍不能回答：

- 共享容器内把同一 periodic total 精确拆给多个 member；
- Agent 常驻进程内部、未派生 child 的工作属于哪个 tool；
- 通用 operation（terminal/file/Git/Fork/snapshot）级 ledger；
- 用 eBPF 做网络/文件授权或计费。

## 2. 问题定义

传统 HTTP trace 常假设一个 request 包含一棵短生命周期调用树。Agent workload 不满足这个假设：

```text
HTTP request
  -> create Job
      -> Attempt 1
          -> long-lived resident Agent CLI
              -> model calls
              -> tool A
                  -> shell
                      -> compiler
                          -> workers
              -> tool B (may overlap)
              -> detached child
      -> Attempt 2 after retry
```

另外：

- HTTP response 可以在 Agent 完成前很久返回；
- 同一个 resident process 可以服务多个 turn；
- PID 会退出和复用；
- 子进程可以 `setsid`、double-fork 或把工作延后；
- Fork sibling 可以在同一个 container/workspace 中并发；
- terminal、file/Git、snapshot 并不一定属于 Agent turn；
- container memory/page cache、network socket 与 Git mutation 可能是 group-scoped。

**核心归属问题：** 对一项被平台承认的 operation，哪些资源和副作用可以精确归属，哪些只能按窗口估计，哪些只能留在 sandbox group 总量中？

## 3. 目标与非目标

### 3.1 目标

1. 每个 Attempt 可查询“发生了什么、消耗了什么、平台做了什么”。
2. Shared sandbox 中不把同一 container counter 重复记到多个 chat/job。
3. 资源值同时携带 measurement coverage 与 attribution quality。
4. Operation identity 在 HTTP、process restart 与 PID reuse 之外保持稳定。
5. Sensor 故障不影响 turn 正确性，但必须显式产生 gap。
6. Kernel/process telemetry 不复制 prompt、tool result 或 credential。
7. Gateway inference ledger、Agent event ledger 与 resource telemetry 各自保持单一 truth。
8. eBPF 只作为 observation sensor，不是 isolation 或 authorization。非 Kata 路径无 guest observer 也能以 Docker stats 工作；已 advertise 的 Kata host 则必须通过 observer preflight，不能悄悄降级后仍宣称 VM-isolated process 级精度。

### 3.2 非目标

- v1 不建设 distributed tracing system；
- 不把 eBPF 作为 sandbox isolation 或 authorization mechanism；
- 不声称精确计量共享 page cache、GPU 或 runtime 内部线程到单个 tool；
- 不用 Agent 自报 CPU/网络作为 authority；
- 不把 resource telemetry 直接变成计费依据；
- 不部署第二套 Prometheus/Grafana estate；
- 不捕获文件内容、prompt 或完整 command environment；
- 不保证任意内核/runtime 上都有同等 eBPF capability。

## 4. Identity 模型

| 层级 | 定义 | 能否重试/复用 |
|---|---|---|
| Thread | 用户长期会话 | 跨多个 job |
| Job / Turn | 一条用户输入 | 可有多个 attempt |
| Attempt | 一次 lease-fenced execution | retry 后新 id |
| Activation | Prompt admission generation | resident process 可跨 activation 复用 |
| Operation | 一个可独立开始、结束、取消和归属的动作 | 有稳定 `operation_id` |
| Process instance | Kernel task/process | PID 可复用，不是业务 identity |

Operation 至少包括：

- `agent_turn`；
- `tool_call`；
- `terminal_session` / `terminal_command`；
- `file_read` / `file_write`；
- `git_read` / `git_mutation`；
- `native_fork`；
- `workspace_snapshot`；
- `setup`；
- trusted `publish`（在 sandbox 外观测）。

Operation 可嵌套：

```text
agent_turn op_turn
  ├── model_call op_model_1
  ├── tool_call op_tool_a
  │     └── process subtree
  └── tool_call op_tool_b
        └── process subtree
```

`operation_id` 由 control plane、Supervisor 或 sandbox manager 生成，不能由不可信 repository 自行声明。

## 5. 三类事实

### 5.1 Ledger：发生了什么

Ledger 是 canonical event stream 的 projection：

- user message；
- assistant output；
- tool use/result；
- usage；
- terminal state；
- continuity/context transition。

Ledger record 携带 `(attempt_id, client_seq)` 作为 dedupe identity。Telemetry export 可以丢失或重复，canonical store 不因 export backend 改变。

### 5.2 Resources：消耗了什么

Resource 是 monotonic counter 或 window rollup：

- CPU time；
- memory current/peak/average；
- PID count；
- block/file I/O；
- network ingress/egress；
- retained writable-layer storage；
- running-idle 与 stopped duration。

Resource record 必须说明测量 scope、窗口、coverage、sampler version 和 attribution quality。

### 5.3 Ops：平台做了什么

Ops signal 没有 canonical transcript 对应项，例如：

- claim loop lag；
- lease takeover；
- native resume/Fork reconciliation；
- sampler start/stop/gap；
- container OOM/exit；
- denied egress；
- snapshot backlog；
- grant mint/renew/revoke degradation；
- slot cleanup ambiguity。

Ops 用于告警与诊断，不能与可求和 ledger event 混为一类。

## 6. 当前采样架构

```text
sandbox (Kata guest)               runner/host                    control plane
────────────────────               ───────────                    ─────────────
Agent CLI + children              ResourceSampler               worker report API
  │                               1 Hz Docker stats              │
  ├─ tool-mark helper ─┐                                         │
  └─ fork/exec/exit ───┴─► guest-root eBPF observer              │
                           /run/freeinference-observer           │
                                  │ privileged exec read          │
                                  ├─ journal + cursor/loss fence  │
                                  └─ window rollup ──────────────┤
                                                                  ▼
event stream  ───────────────────────────────────────────> agent_job_events
attempt window ── resource rollup ───────────────────────> agent_attempt_resources
tool windows + process rows ─────────────────────────────> agent_tool_call_resources
                                                           agent_process_observations

gateway model calls ─────────────────────────────────────> gateway api_logs
                                                          external job id 是 join key；
                                                          Cloud Agent read path 尚未接通
```

Guest observer 只存在于 Kata。非 Kata 开发路径仍用 Docker stats；Claude Code 的 mark 退回旧的 bounded state-file reader。Kata 从不把 agent-writable 文件当作归属权威。

### 6.1 为什么通过 Docker daemon 采样

Runner 自身运行在 container 中，没有 host PID namespace、host `/proc` 或 cgroupfs。直接从 runner 解析 host PID/cgroup 会在真实部署中系统性失败。

当前 sampler 使用：

```text
GET /containers/{id}/stats?stream=false&one-shot=true
```

优点：

- Runner 已有 Docker API authority；
- 不要求额外 `pid: host`、cgroup namespace 或 mount；
- 本地 host、containerized runner 与远端 Docker daemon 语义一致；
- Sandbox 无法看到、拒绝或伪造采样。

### 6.2 当前 counter 语义

- CPU 使用 daemon cumulative counter 做窗口 delta；
- Memory 与 Docker CLI 对齐，排除 `inactive_file`；
- I/O row 不可用时记为 unmeasured/null，不记 0；
- Sampler window 使用 host monotonic time；
- 一个成功 reading 贡献 measurement coverage；heartbeat 本身不算 sample；
- sampler failure 不终止 turn。

### 6.3 当前限制

Docker stats 的最小 scope 是 container。一个 shared sandbox 同时运行 A/B 时：

```text
container total != member A total + member B total known exactly
```

现有实现的正确行为是：

- group/container metrics 可以继续记录；
- per-member attempt resource 返回 nullable counters + `coverage=0`；
- 禁止把同一 group total 写到 A 与 B 两行；
- runtime-reported token usage 仍可按 activation event 归属；
- Gateway inference cost 仍以 thread/job attribution 记录。

## 7. Coverage 与归属质量

单一 `coverage` 不足以同时表达“有没有测到”和“测到后能否归属”。目标 schema 分开两类质量：

| 字段 | 含义 |
|---|---|
| `measurement_coverage` | operation window 中有有效 sensor sample 的时间占比 |
| `attribution_coverage` | 已测总量中能绑定到该 operation/member 的比例 |
| `boundary_quality` | `exact_process` / `exact_runtime` / `message_window` / `member_window` / `group_only` / `none` |
| `sampler_version` | 计数语义版本 |
| `missing_reason` | bounded enum，例如 `sensor_down`、`shared_scope`、`runtime_no_boundary` |

示例：

- `measurement=1, attribution=0, group_only`：container 全程测到，但共享并发时无法分给 member；
- `measurement=.7, attribution=1, exact_process`：该 process tree 可精确归属，但 sensor 中途缺失；
- `measurement=1, attribution=.4, message_window`：资源总量完整，只有部分落入近似 tool window；
- `measurement=0`：没有测量，所有 counter 为 null，而不是 0。

## 8. Operation ledger 提案

在引入更细 sensor 前，先建立稳定业务边界。建议增加 canonical operation record：

```text
agent_operations
  operation_id primary key
  operation_kind
  parent_operation_id nullable
  sandbox_id
  thread_id nullable
  job_id nullable
  attempt_id nullable
  membership_generation nullable
  activation_generation nullable
  lifecycle_generation
  state: preparing | active | settled | cancelled | failed | abandoned
  started_at / ended_at
  start_mono / end_mono (host receipt)
  boundary_source
  error_class nullable
```

规则：

1. Control plane 创建 intent；Host receipt 提供真实 local monotonic boundary。
2. `operation_id` 随 retry 保持或明确创建 successor，不能复用旧 generation。
3. Process telemetry 只引用 operation key，不携带完整业务 payload。
4. Operation state 不能由 eBPF event 单独推进；eBPF 是 observation，不是 lifecycle authority。
5. Unknown/background work 可以绑定 sandbox/member，但 `operation_id=null`，进入 unclassified bucket。
6. Fork、snapshot 等已有 operation id 与 saga id 应复用，不再创建平行 identity。

## 9. 分层测量方案

### Layer 0：Group/container counter window（部分已实现）

当前数据源是 Docker Engine stats。

已实现：

- 可诚实绑定一个 attempt window 时的 CPU/memory/PID/I/O/network counter；
- group/container scope total 与 measurement coverage；
- shared member 无法独占归属时返回 nullable counter + `coverage=0`，不复制 group total。

尚未实现：

- running-idle 与 stopped duration rollup；
- retained writable-layer size/增长量；
- 独立的 container OOM/exit event 与 reason；
- 共享 group 内 periodic member/operation split。

### Layer 1：Runtime/event windows（已实现）

数据源：Agent CLI event、`tool_use`/`tool_result`、Claude Code 的被动 PreToolUse/PostToolUse hook。

Boundary capability：

| Capability | 边界 | 当前 runtime |
|---|---|---|
| `exact` | 平台 hook 在 sandbox 内标记 execution start/end | Claude Code：image-bundled helper 向 guest observer socket 发 bounded mark |
| `message` | 从 `tool_use` arrival 到 matching `tool_result` arrival | Codex、无 hook 的 Claude、Pi 等 |
| `none` | 无可靠 tool boundary，只保留 turn/member/group total | Kilo / OpenCode |

`message` 包含 transport 和模型等待时间，必须显示为 approximate。Hook 必须 fail-open：记不上 mark 时窗口降为 `message`，不能挡住 tool。

Claude Code 的 helper 是 immutable、非 root；observer 用内核提供的 peer credential，并核对 `/proc/<pid>/exe` 是固定 helper inode。同一 UID 的 Agent 代码仍能自己调用 helper，因此 exact timing 是诊断，不是安全或计费边界。进程普查仍是“出现过哪些进程”的权威。

### Layer 2：Member/process-tree sampler（Shared mode 的最低补全，仍部分未实现）

Kata 已用 guest UID 过滤进程事件，因此 shared-live 的进程普查是 per-member 的。Periodic CPU/memory/I/O 仍不能从 hypervisor/container total 诚实拆给 member；该层的 periodic 补全仍未做。

数据源可组合：

- 已落地：member UID 的 guest 进程普查 + exit-time counters；
- 未落地：host `/proc` 或 cgroup 级 per-member periodic sampler（Kata 上本来就看不到 guest `/proc`）。

Memory/page cache 和跨进程 FD 仍可能只能 group-scoped。

### Layer 3：eBPF process sensor（Kata guest 已落地；host sensor 仍是提案）

生产路径是 **guest-root CO-RE observer**，不是本文初稿里的 host OperationSensor daemon。这与沙盒安全模型一致：Kata 上 Host eBPF 只能看到 VMM/virtio，process/syscall 级观测必须进入 guest。

它补足短进程可见性与 exit-time 计数，不替换 Layer 0/1，也不推进 operation lifecycle。BPF 不知道 `thread_id` / `operation_id`；归属发生在 host parser：turn cursor + member UID + tool window。

## 10. eBPF：已落地的 guest observer

### 10.1 为什么不是 host daemon

初稿把 sensor 放在宿主机，假设 runner 能看见工具进程的 syscall。生产执行档位是 probed Kata：

- 工具 syscall 由 guest Linux 执行；
- Host eBPF 看不到 guest PID、comm、RSS 或 I/O；
- 给 runner 加 `pid: host` / `CAP_BPF` 既解决不了 guest 不可见，也会扩大 TCB。

因此落地形态是：审核过的 guest 镜像 helper，由 Kata-only privileged exec 拉起；Agent 进程保持 drop-capability 的 member UID，永不持有 `CAP_BPF`。

### 10.2 已落地部署位置

```text
Kata guest kernel
  └── CO-RE BPF programs + ring buffer
           │ fork / exec / exit / TASK_DEAD sched_switch
  /usr/local/libexec/freeinference-guest-observer  (guest root)
           │ root-only JSONL journal + status + tool-mark socket
           │ /run/freeinference-observer
  privileged Docker exec (host, fixed argv)
           │
  GuestObserverController  (cloud_agent_host/guest_observer.py)
           │ cursor / UID / loss / inode fence
           │
  worker report → agent_attempt_resources
                  agent_tool_call_resources
                  process observation rows
```

约束：

- Controller 只接收两个固定 exec seam，不接受 job argv；
- Journal 为 root-only、append-only、UID 过滤，带 byte/event/inode fence；
- 硬顶 64 MiB；retained/shared 在无 turn cursor 时于 48 MiB 轮转；单次 turn 最多读 4 MiB / 4096 进程 / 1000 marks；
- Ring-buffer 丢失、畸形记录、重启、截断都使结果 **partial**，从不渲染成“完整的零进程普查”或独占资源数；
- `source` 固定为 `kata-guest-ebpf-v1`。

一次性、retained/HOT、shared-live / native-fork 共用同一 cursor/loss 契约。Shared turn 按 per-member guest UID 划界。

### 10.3 已落地事件与计数

四个 probe：

| Probe | 记录 | 读什么 |
|---|---|---|
| `sched_process_fork` | `FI_PROCESS_FORK` | 新 process-leader 的 pid/tgid/ppid/uid/comm |
| `sched_process_exec` | `FI_PROCESS_EXEC` | basename / executable path（长度受限） |
| `sched_process_exit` | `FI_PROCESS_EXIT` | `exit_code`、group `maxrss`、`task_io_accounting`、`signal->live == 0` |
| `sched_switch`（仅 `TASK_DEAD`） | `FI_PROCESS_DONE` | `se.sum_exec_runtime` |

CPU **不** 从 `utime`/`stime` 读：tick accounting 会让亚毫秒进程报 0。`sum_exec_runtime` 只在任务最后一次出队后才最终，所以放在 `TASK_DEAD`。`sched_switch` 上其余切换只做这个谓词。

每个字段用 `bpf_core_field_exists` 守卫。内核没有 task I/O accounting 时程序仍能加载，该计数记为 absent，不是 0。

Host 把同一 thread group 的线程折入 leader：CPU 与 bytes 求和，peak 取最大。`signal->utime` 故意不加，避免死线程被算两次。Group 只在 **已死且每个见过 exit 的 task 都有 `done`** 时结算——Linux 允许 leader 先退、线程继续跑，leader 的 exit 不是进程结束。

线程本身不进入普查表。Marker helper 的生命周期可见，但其 executable identity 排除在 per-tool workload 计数之外。

`--self-test` / Kata preflight 必须经过 ring buffer 看到一个真实的短命非 root child 的 fork、exec、exit，并且 `done` 带非零 CPU、exit 带非零 peak RSS 且 group dead。BTF 无法重定位这些字段的 guest kernel 不能宣称可接活。Storage/byte accounting 缺失只警告。

### 10.4 已落地归属

Census 是 **turn cursor 之后观察到的进程启动**，不是“当时已经住在容器里的进程快照”。

`add_process_resources` 规则：

1. 进程只归属恰好一个 tool window 且 group 已结算 → 整段消耗记入该 call；
2. 多个重叠 window 都说得通、group 未结算、或 census 丢过事件 → 不记入任何一个 call，相关 call 标 `process_resources_partial`；
3. CPU/I/O 对已归属进程求和；peak 取单个进程最大值（峰值不必同时发生）；
4. Agent 常驻进程内部的工作不属于任何 call，UI 必须说明；
5. 截断的 tool list 禁止 per-call 资源与 time split：被省略的 call 可能与每个保留窗口重叠。

Hook 标记的采用规则（2026-09-22 起）：

1. Kata 路径的标记由 guest observer 记录在 guest 单调时钟上，与进程事件同一时钟。采用只验证配对：标记指向本 turn 发出的调用、恰好一对 pre/post、pre 早于 post。
2. 消息级窗口不再作为标记的边界。sandbox manager 在读到那一行时才打时间戳，短调用可能在这一行还在管道里时就已经跑完。窗口相对标记的偏差按调用记入 sandbox manager 日志：`tool marks thread=… calls=[seq:tool:status:lag=open/close:msg=…:exact=…]`，status 取 `applied`、`no_marks`、`unpaired`、`duplicate`、`misordered`、`call_open`、`outside_window` 之一；指向本 turn 没有保留窗口的标记计入 `unknown_calls`。
3. 非 Kata 的墙钟标记文件保留旧规则：两个标记必须落在消息窗口内。
4. 采用了 guest 标记的窗口在归属进程时直接比较 guest 时钟，不再加时钟桥误差；消息级窗口仍按误差放宽。

这修复的是 **短进程可观测性** 和 **任意时长调用的 exit-time 计数**。它不把 Docker 1 Hz 采样伪装成亚秒测量。Sampler 仍是 turn-level total 与 runc 的来源。

展示：

- 计时写明 `exact` sandbox timing 还是 `message` 外括号；
- Shared periodic counter 从不写成独占；
- Lossy process count 写成下界（`>=N`）；
- Footer 是 tool-bracketed time vs outside-tool time（model + harness/platform），不宣称 remainder 是纯模型时间；
- 只标出 **longest** call，不标 **heaviest**——多数 call 没有可辩护的 CPU/memory 排序；
- 模糊 block I/O 继续隐藏。

### 10.5 与初稿 host-sensor 提案的差异

| 初稿提案 | 实际落地 |
|---|---|
| Host kernel BPF + host OperationSensor daemon | Guest kernel CO-RE BPF + guest-root loader |
| `operation_id` → 短 `operation_key` 写入 `bpf_task_storage` | BPF 不持有业务 id；host 用 UID + cursor + tool window join |
| pidfd + start-time 注册 operation root | Privileged exec 拉起固定 helper；无 per-operation BPF map |
| 计划中的 connect/OOM/VFS 探针 | 未做。四个 sched probe + tool-mark socket |
| “当前代码中没有 BPF program” | `deploy/sandbox/guest-observer.{bpf.c,c,h}` |

初稿里“eBPF 不能从 PID 推断 `thread_id`”仍然成立，所以 **没有** 把业务 identity 推进 BPF map。绑定留在 userspace。这比提案更窄，也更诚实。

### 10.6 Memory 与 I/O 诚实边界（仍适用）

eBPF 仍不应宣称能把 shared address space、page cache 与 allocator memory 低成本精确分给 tool。

当前分层：

- container / VM memory：Docker stats，group truth；Kata periodic 常为 unavailable；
- 已退出 child 的 peak RSS：exit-time `maxrss`，按 call 独占归属或标 partial；
- resident Agent CLI 自身 memory：不属于任何 tool call；
- page cache / shared memory：group-only；
- `read_bytes`/`write_bytes`：落到存储的字节；page cache 命中的读可以是 `rchar > 0` 且 `read_bytes = 0`，UI 必须区分。

禁止为了“完整”而让 operation memory 总和超过真实 container peak 并仍标记 exact。

### 10.7 仍属提案

这些没有随 #89/#102 落地，也不应写成已交付：

- 通用 `agent_operations` ledger 与 `operation_id` 注入 BPF；
- Host-side OperationSensor（runc 上理论上仍可做，生产 Kata 不需要）；
- 网络 connect/skb eBPF、egress flow parser、unclassified byte ledger；
- 全量 VFS path event / OOM tracepoint；
- 把 resident CLI 内部线程拆到重叠 tool。

网络仍只有三种已有 authority：Gateway ledger、MCP relay、allowlist/proxy。eBPF 网络字节不能推导模型费用。

### 10.8 Long-lived resident process

已落地策略比初稿更窄，也更可执行：

- 进程事件按 member UID 过滤；cursor 在 prompt 发出前打上；
- cursor 之前已存在的 resident CLI 不进入本 turn 普查；
- 它在窗口内派生的 child 进入普查，并按 tool window 归属；
- 它自己的 CPU/RSS 不记入任何 tool call；
- Turn 后仍存活的未授权 child 仍由 Supervisor reaper 清理；
- terminal / dev server 若跨 turn 存活，不会仅仅因为还在跑就被算进下一个 prompt。

## 11. Window 与 overlap 规则

Sampler interval 可能同时覆盖多个 operation。Periodic 归属规则：

- 一个 exact process owner：记入 `exclusive_*`；
- 多个 message window 重叠：每个记录同一 shared delta，并携带 `overlap_n`；
- shared delta 不能参与求和；
- 没有 operation boundary：留在 attempt/member/group residual；
- Agent CLI/model streaming 本身的资源归到 turn，不强塞给最近 tool；
- Fork/snapshot 等平台 operation 与 Agent turn 并发时，按 process owner 或 group operation ref 分开；无法区分则 shared。

Kata exit-time 计数另走 §10.4：只给“恰好一个 window 且 group 已结算”的进程收费；重叠或丢失标 partial，不写成 exclusive。

展示层必须使用以下词义：

| 展示 | 含义 |
|---|---|
| exact | Process/runtime boundary 直接证明 |
| approximate | Message/window correlation |
| shared upper bound | 与其他 operation 重叠，不可求和 |
| group only | 只知道 sandbox 总量 |
| incomplete | measurement coverage < 1 |
| unavailable | counter 为 null，不是 0 |

## 12. 数据模型提案

### 12.1 Attempt rollup（当前表扩展）

```text
agent_attempt_resources
  attempt_id primary key
  cpu_usec nullable
  mem_peak_bytes nullable
  mem_avg_bytes nullable
  io_read_bytes nullable
  io_write_bytes nullable
  net_rx_bytes nullable
  net_tx_bytes nullable
  sample_count
  measurement_coverage
  attribution_coverage
  boundary_quality
  sampler_version
  missing_reason nullable
  created_at
```

### 12.2 Operation rollup（仍属提案）

```text
agent_operation_resources
  operation_id primary key
  resource_scope: operation | member | group
  cpu_usec_exclusive nullable
  cpu_usec_shared nullable
  process_rss_peak_bytes nullable
  io_read_bytes_exclusive nullable
  io_write_bytes_exclusive nullable
  net_bytes_classified nullable
  overlap_n_max
  measurement_coverage
  attribution_coverage
  boundary_quality
  sensor_version
  truncated
```

### 12.3 Process projection（已落地，挂在 attempt 上）

当前没有 `agent_operations`，进程行挂在 attempt 资源上，不挂不存在的 `operation_id`。

```text
ProcessObservation / WorkerProcessObservation
  process_seq
  uid, pid, tgid, ppid nullable
  comm, executable nullable
  started_offset_ms          # 相对 turn cursor，不是 wall clock
  duration_ms nullable
  exit_code nullable
  observed_fork / observed_exec / observed_exit
  tool_call_seqs[]           # 重叠时保留所有说得通的 call，不发明唯一 owner
  cpu_usec / mem_peak_bytes / io_* nullable
```

`ToolCallResources` 另有 `process_count`、`process_cpu_usec`、`process_mem_peak_bytes`、`process_io_*`、`process_resources_partial`。没有 observed exit 不能带 counters；没有 attributed process 不能带 per-call totals；两者都要求 named observer。

不建议永久存储每次 `sched_switch` 或每个 syscall。Kernel event 在 guest loader 中写成 bounded JSONL，host 再 roll up；只有窗口结果写入 Postgres。

通用 `agent_operation_processes`（按 `operation_id`）仍是提案。

### 12.4 Egress flow

```text
agent_egress_flows
  sandbox_id
  thread_id nullable
  attempt_id nullable
  operation_id nullable
  started_at / ended_at
  destination_class
  target_host_redacted
  verdict
  bytes_in nullable
  bytes_out nullable
  attribution_quality
```

## 13. Gateway cost 与 resource telemetry 的关系

Gateway `api_logs` 是 inference token/cost 的唯一 billing authority。Agent resource telemetry：

- 可以显示 CPU、memory、I/O、network 与 runtime-reported usage；
- 可以通过 `external_job_id` join Gateway cost projection；
- 不能覆盖 Gateway token/cost；
- 不能用 eBPF network bytes 推导模型费用；
- 不能把跨 turn 复用 credential 的 Gateway usage 伪拆成不存在的 per-tool cost；
- 如未来用于配额或计费，需要独立精度、争议处理和审计设计。

## 14. Cardinality、隐私与保留

### 14.1 Bounded dimensions

Metrics/aggregate label 只允许 bounded enum：

- host class；
- runtime；
- context/continuity state；
- operation kind；
- terminal/error class；
- egress verdict；
- boundary quality；
- known tool name 或 `other`。

`thread_id`、`attempt_id`、`operation_id`、domain、path 与 PID 不作为 metrics label；它们只进入 indexed row 或 exemplar。

### 14.2 Redaction

共享 telemetry sink 默认 deny body：

- 不导出 prompt、assistant text、tool args/results；
- 不导出 argv、environment、credential、file content；
- executable/path/domain 经过 allowlist、分类或 hash/redaction；
- redaction failure 按 record fail closed，不影响 canonical DB；
- event size/type/severity/identity 可导出。

### 14.3 Retention

建议：

- Raw 1 Hz / kernel event：host ring buffer，窗口关闭后丢弃；
- Process lifecycle/security event：短期保留；
- Attempt/operation rollup：长期保留；
- Proxy raw log（setup allowlist proxy 接线后）：短期保留，解析后删除；
- Gateway billing ledger：按 Gateway retention policy；
- 研究数据集导出必须再做去标识化与访问控制。

## 15. Failure 与降级矩阵

| 情况 | Turn 行为 | Telemetry 行为 |
|---|---|---|
| Docker sampler 中途失败 | 不影响 turn | `measurement_coverage < 1`，counter 按已测部分或 null |
| Shared mode 无 member sampler | 不影响 turn | per-member `coverage=0`；group total 保留 |
| Guest eBPF 不可加载/BTF 不兼容 | Kata host 不能 advertise 可接活 | Preflight `--self-test` fail closed；非 Kata 仍走 Layer 0/1 |
| Observer journal 丢失/截断/restart | 不影响 turn | `process_observation_complete=false`；counts 为下界；不得渲染完整零普查 |
| Tool list 被截断 | 不影响 turn | 禁止 per-call 资源与 time split |
| PID reuse | 不影响 turn | cursor + journal inode/dev + start offset；无法证明则不归属 |
| Hook 缺失 | Tool 正常执行 | `message` 或 `none` |
| Hook 写失败/超时 | Tool 必须继续 | 降为 `message`；helper 25 ms stdin + 1 s CLI timeout，fail-open |
| Parallel tool calls | 正常 | sampler 用 `shared_*` + `overlap_n`；exit-time 对重叠进程不收费并标 partial |
| Egress log parse gap | 正常 | 当前无 parser；不假装有 unclassified ledger |
| Sub-second call 无 Docker sample | 正常 | `sample_count < 2` 则 periodic 为 null；Kata 仍可用 exit-time counters |
| Container OOM | Group failure | 当前只有 container 终止与最后可用 sample，没有独立 OOM event；该 event/reason 仍需新增，所有 member 均受影响 |
| Collector queue 满 | 正常 | 丢低优先级 raw event、保留 gap counter，不 block sandbox |
| Postgres report 重试 | 正常 | `(operation_id, kind/version)` 幂等 upsert |

## 16. 安全约束

Guest eBPF 把一小块特权放进 Kata guest，必须满足：

1. Agent 进程永不持有 `CAP_BPF`、host `/proc`、cgroupfs 或任意 BPF loader。
2. 只有审核过的镜像 helper 能通过 Kata-only privileged exec 启动；exec argv 固定，不来自 job。
3. Journal 与 mark socket 对 member UID 不可写；mark 必须核内核 peer cred 与固定 helper inode。
4. Runner 不可上传任意 BPF program 或修改 filter code。
5. Ring buffer / JSONL 不含 prompt、tool result、credential、argv 或 environment。
6. Sensor failure 不放宽 network、credential 或 process isolation；hook failure 不挡住 tool。
7. File/network observation（若未来增加）不被解释为 authorization。
8. Kernel compatibility 与 verifier failure 必须可见：Kata preflight fail closed，不得 advertise 不存在的精度。
9. Journal 有硬顶与轮转；单次 turn 有读上限；超限报 incomplete，不静默丢进程。
10. BPF program、loader 与 CO-RE artifact 纳入镜像 pin、attestation 和升级回滚。

## 17. Product 与运维 surface

### 17.1 Thread UI

每个 settled turn 可以显示：

- context status；
- duration；
- Gateway inference cost（若 join 可用）；
- CPU、peak memory（periodic；Kata 常为 unavailable）；
- tool-bracketed time vs outside-tool time；
- measurement/attribution badge。

展开后（已落地）：

- per-tool 表：duration、`exact`/`message`、periodic 值或 dash、exit-time CPU/peak（标记 `ᵖ`）、lossy 前缀 `≥`；
- guest process 表：comm、CPU、peak RSS、I/O；不完整 census 的 count 为下界；
- longest call，不是 heaviest；
- sampler gap 与 missing reason。

未落地、不得假装已有：

- 通用 operation timeline；
- per-domain network panel 与 denied CONNECT 计数；
- 把 shared periodic total 写成 member exclusive。

Shared workspace 下，container group total 与 member/turn total 必须分开展示。

### 17.2 Ops/Admin（延后）

未来 aggregate surface 可包括：

- claim/resume/fork failure；
- live/restored/lost funnel；
- sampler/attribution coverage；
- container capacity 与 stopped disk；
- denied egress 与 unclassified growth；
- OOM、unknown writer、cleanup ambiguity；
- operation latency 与 resource outlier。

这些 projection 读取 Postgres，不要求先部署 Prometheus/Grafana。OTLP 只作为可插拔 export seam。

## 18. 分阶段实现

### OBS-0：Contract 与基线

- 固化当前 Docker Engine counter 语义；
- 区分 nullable counter 与 measured zero；
- 将 `coverage` 拆为 measurement/attribution quality；
- 添加 shared mode 不重复 container total 的测试；
- 定义 bounded dimension/redaction allowlist。

### OBS-1：Operation ledger

- 统一 turn/tool/terminal/file/Git/Fork/snapshot operation id；
- Control plane intent + host monotonic receipt；
- Sandbox manager/Supervisor generation fence；
- UI/API 暴露 boundary source 与 missing reason。

### OBS-2：Member process accounting

- UID + PID/start-time census；
- member CPU/RSS/I/O periodic sampler；
- background/unclassified process detection；
- 与 Docker group total conservation；
- shared-mode per-member coverage 从 0 提升。

### OBS-3：Runtime/tool boundary（已落地）

- Claude Code exact hook + 其余 runtime 的 message fallback；
- overlap/shared rollup；
- `agent_tool_call_resources` 与 footer UI。

### OBS-4：Kata guest eBPF（代码已落地，staging 验收未关）

- Guest-root CO-RE observer：fork/exec/exit/`TASK_DEAD`；
- Exit-time CPU/RSS/I/O 与 per-call 独占归属；
- Cursor/loss/inode fence；shared-live UID 划界；
- Preflight `--self-test` fail closed；
- 仍缺：一台真实 staging Kata/KVM、debug/BTF guest kernel 上的验收。

初稿的 host OperationSensor / `operation_key` map **不再是本阶段目标**。

### OBS-5：eBPF production gate（未关）

- 真实 Kata host 与隔离 runtime 验证；
- journal 轮转、crash、backpressure；
- shared resident 与并行 tool stress；
- 镜像 pin 与 rollback。

### OBS-6：仍属提案

- 通用 operation ledger；
- Host-side sensor（仅当需要 runc process 级观测时）；
- Proxy flow parser 与 unclassified byte ledger；
- 后续 admin aggregate/alerts。

## 19. 验收标准

已由 #89 / #102 与现有 sampler 覆盖：

1. 一个未测量的 turn 返回 null counters + coverage 0，不返回全 0。
2. Shared A/B 并发时，container total 不会复制到两个 attempt。
3. Parallel / 重叠 call 的进程不进入任何一个 call 的独占 exit-time 总和，并标 `process_resources_partial`。
4. Census 丢失、journal 截断或 observer 不可用时，不渲染完整零进程表，也不 advertise exact。
5. Cursor 之前已存在的 resident CLI 不进入本 turn 普查；其内部工作不属于任何 call。
6. Hook 失败不挡住 tool；窗口降为 `message`。
7. Kata preflight 看不到真实短进程的 fork/exec/exit/`done` 时，host 不得宣称可接活。
8. File/network/process telemetry 不包含 prompt、credential、argv/environment 或 file content。
9. Raw kernel event 不无限写 Postgres；journal 与 per-turn 读上限生效。

仍属提案或 staging 门禁：

10. 一台真实 staging Kata/KVM 上跑通 observer + debug/BTF guest kernel。
11. Gateway inference cost 与 Agent resource row join 后仍各自可追溯到原 authority。
12. 通用 operation id（terminal/dev server/Fork/snapshot）不继承过期 activation。
13. 若未来加 host sensor：stale generation 的 process registration 被拒绝；BPF overhead 可独立关闭。

## 20. 实现导航

### 当前已落地

| 能力 | 路径 |
|---|---|
| Docker Engine sampler | `cloud_agent_host/resources.py` |
| Runner window/report | `cloud_agent_host/runner.py` |
| Tool window / hook marks | `cloud_agent_host/tool_windows.py`、`deploy/sandbox/tool-mark.c` |
| Kata guest observer | `deploy/sandbox/guest-observer.{bpf.c,c,h}` |
| Host journal parser / 归属 | `cloud_agent_host/guest_observer.py` |
| Sandbox start / preflight | `cloud_agent_host/sandbox.py`、`cloud_agent_host/chat_sandbox.py` |
| Shared-live UID 划界 | `cloud_agent_host/shared_session_supervisor.py` |
| Attempt / tool / process schema | `cloud_agent/schemas.py`（`AttemptResources`、`ToolCallResources`、`ProcessObservation`） |
| Store + migrations | `cloud_agent/storage/store.py`、`migrations/versions/0021_tool_call_resources.py`、`0022_process_exit_counters.py` |
| Footer UI | `web/src/components/features/agents/TurnFooter.tsx` |
| Canonical Agent event | `cloud_agent/storage/store.py` |
| Gateway job cost attribution | HybridInference `apps/backend/serving/storage/log_schema.py`、`apps/backend/serving/servers/routers/agent_grants.py` |

### 仍需新增

- `agent_operations` 与跨 turn/tool/terminal/file/Git/Fork/snapshot 的统一 identity；
- Host-side sensor（仅 runc 需要 process 级观测时）；
- Egress flow parser 与 byte conservation projection；
- Staging Kata/KVM 上关闭 OBS-5。

对外可以声明：Kata 上有 guest eBPF 进程普查与 exit-time 子进程资源，且丢失会显式标 partial。不能声明：通用 operation 级精确归属、host BPF、或把 periodic container total 拆给 shared member。
