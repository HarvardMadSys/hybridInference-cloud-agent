# 沙盒启动时间测量设计

创建：2026-09-14；交付范围更新：2026-09-18。
状态：**#258 已实现开关、run descriptor、分阶段计时、四条启动路径分类、关键内部阶段、
采集器微基准、files/terminal 服务端接入、Postgres、Admin API/UI 与有界投递**；
浏览器终点和端到端 benchmark 报告仍未实现。
初始路径检查基于 checkout `01cfbae`；当前交付核对基于合并最新 dev 后的 #258 分支，
不代表线上耗时或已经部署。

实现现状见 [§9 实施顺序与验收](#9-实施顺序与验收)：本文的字段语义、时钟与失败处理
仍是后续切片的目标形态，已实现的子集以 `apps/backend/cloud_agent_shared/operation_profiling.py` 为准。

**2026-09-18 整合结果**：#218 的持久化/API/Admin 能力已接入 #258 的唯一采集链路。
[存储/Admin 整合设计](2026-09-16-operation-measurement-storage-admin-integration-design.zh-CN.md)
规定新的记录身份、覆盖范围、异步写入、鉴权、幂等和交付顺序；与本文的早期
存储草案或实施顺序有差异时以整合设计为准。`e77e3e6` 的历史
[五项 review 问题](2026-09-16-operation-measurement-e77e3e6-review.zh-CN.md)已关闭。

目标：对每次启动回答三个问题：**总共等了多久、时间花在哪里、某个优化是否真的有效**。
完整目标包含启动摘要、单次启动时间线和可重复的 benchmark；#258 以结构化日志、
可选 JSONL、Postgres 与 Admin 承载同一冻结记录。

**原始首批需求还包括文件浏览/打开与终端打开。** 它们共享开关、时钟、call ID、
完整性与异步导出机制，区别在操作入口和完成条件。当前 attempt.startup 只覆盖
runner claim 请求前到 prompt 交给 supervisor，不包含 CP 队列等待、浏览器渲染或模型
首输出，也捕获不到没有新 attempt 的文件/终端操作。因此 #258 的启动测量完成，
不能解释为三类需求全部完成。文件 benchmark 也不等于真实用户请求的 profiling。
文件/终端已实现 **CP 选择与 broker 阶段**（见 §9.2），但浏览器终点仍未接入：
交付口径是 server-only 片段，不能声称已覆盖“点击到可用”。完整状态、命名、前端接入、
缓存/重试/就绪边界和整体验收详见
[平台操作测量清单 §3–§5](2026-09-14-platform-operation-measurement-inventory.zh-CN.md)。

**2026-09-14 修订：本方案用于按需 profiling，默认关闭。** 下文“每次启动”限定为
显式开启实验且被选中的启动；不进行长期常驻全量采集。统一开关、目标范围、截止时间、
样本上限、关闭路径成本和自身开销验证以
[平台测量清单 §0](2026-09-14-platform-operation-measurement-inventory.zh-CN.md#0-可选-profiling-开关)
为准。开关仅管新增平台 profiling，现有资源观测状态单独记录并在对比实验中保持一致。

## 1. 现有记录为什么不够

| 代码事实 | 对测量的影响 |
| --- | --- |
| `runner._run_shared_live_attempt()` 有 `stage` 和 `started`、`checked_out` 等 lifecycle 事件 | 有阶段语义，但没有阶段起止时刻；`stage` 目前主要用于失败定位 |
| activity 的 `created_at` 是 CP 接收时间 | 两个事件的间隔含 HTTP、重试和接收延迟，不能当成实际阶段耗时 |
| `agent_attempts.started_at` 在 claim 插入记录时产生 | 不是容器启动或 CLI 开始执行的时刻 |
| `run_forever()` 默认每轮后 `idle_sleep_s=5.0` | 排队可能包含轮询等待；不能归入沙盒创建，也不能假设每次都固定增加 5 秒 |
| 新建路径先在 setup 网络启动，再切换到 agent 网络 | `docker start` 返回后还有仓库、setup、权限和网络准备 |
| Kata 的 `transition_to_agent_network()` 会 stop guest、修改网络、再 start | 只测首次 `docker start` 会漏掉第二次启动；需要把它从网络操作中拆出来 |
| `_ensure_transport()` 可能复用 CLI，也可能因进程退出、配置或凭据变化重开 | `context_status` 本身不足以确定实际启动路径 |
| `ResidentTransportFactory.open()` 调用各 driver 的 `ensure_started()` | Codex 等有协议初始化；当前 Claude 只 spawn 后检查 `poll()`，就绪证据不同 |
| `SharedChatSessionSupervisor.start_turn()` 在发送 prompt 前还做进程 census、observer/window 准备 | resident 返回不等于本轮已发送 prompt，现有资源观测也可能产生启动开销 |

这些是埋点依据，**不是已经确认的性能瓶颈**。部署时的镜像构建、拉取、host
preflight/enrollment 与每个任务的启动分开测；若运行时发生镜像缺失或获取，必须显式记录。

## 2. 先固定测量边界

不使用一个含义不明的 `startup_time` 字段。主视图同时展示以下指标，并注明计时来源。

| 指标 | 起点 → 终点 | 回答的问题 |
| --- | --- | --- |
| `queue_wait_ms` | 当前入队周期的 DB 入队时间 → DB 分配 attempt 的时间 | 等待调度多久；含同 chat 前序 turn、容量、host pin、轮询等等待 |
| `claim_rpc_ms` | runner 发出 claim 请求 → 解析出成功 claim 响应 | 本次领取请求多慢；CP 侧再拆 DB claim、grant、repo entitlement 和 clone credential |
| `sandbox_prepare_ms` | runner 得到 claim → workspace、runtime 检查及最终 agent 网络验证通过 | 分配后多久具备平台定义的沙盒运行条件；**沙盒优化的主指标** |
| `resident_prepare_ms` | runner 首次开始 resident 获取 → 获得最终 resident descriptor | CLI 复用/初始化多久；包含失败的复用尝试、重开 grant 和重试 |
| `host_to_dispatch_observed_ms` | runner 得到 claim → 收到 broker 的 prompt dispatch 通知 | 平台多久把本轮任务送进 CLI；含通知返程开销 |
| `first_visible_ms` | 浏览器提交本轮操作 → 首个本轮 assistant 文本或工具活动被渲染 | 用户实际等多久；覆盖提交、排队、启动、模型和事件传输 |

`sandbox_prepare_ms` 的终点放在 runner 的 `agent_network` 成功之后、
`terminal_resume` 之前。它表示**现有平台检查通过**，不声称已完成 CLI 协议握手。
复用路径同样计实际验证耗时。benchmark 可额外执行固定无副作用命令验证 guest 可执行；
其耗时单列为 `readiness_probe`，不在生产每次启动时新增 Docker probe。

`resident_prepare_ms` 必须携带 `readiness_source`：

- `protocol_ack`：driver 收到明确初始化或 native session 握手响应。
- `process_alive`：当前实现只确认进程未退出；Claude 目前属于此类。
- `reused_process`：复用相同 process generation 的兼容存活 transport。
- `unknown`：证据缺失，不能补成协议就绪。

不同来源分别聚合。记录各 driver 的实际协议边界，不为计时发送额外模型 prompt。
写入 pipe 或提交 HTTP prompt 后记录 `prompt_dispatched`，仅表示 transport 已发送，
不能命名为“模型收到请求”。有原生 acceptance ack 时另记 `prompt_accepted`；无 ack 留空。

补充两个诊断指标：broker 本地的 `dispatch_to_first_activity_ms` 与
`dispatch_to_first_text_ms`。前者允许工具活动，后者仅 assistant 非空文本；排除
lifecycle、system init、heartbeat、usage、历史 replay 和原样回显的用户 prompt。
两者都包含 CLI/工具/模型等待，不命名为 Gateway TTFT。真正模型请求的 TTFT 仅来自
Gateway 的请求记录，不从 agent 事件间隔推断。

浏览器使用本地单调时钟和本轮 job/attempt 关联；页面刷新、重新附着和后台页面样本
分别标记，不伪造从提交开始的完整测量。排队取消但没有 attempt 的任务也留在排队统计中。

### 时间线

```mermaid
flowchart LR
    A[本轮提交] --> B[入队 / 分配 attempt]
    B --> C[runner 收到 claim]
    C --> D[容器 / workspace / setup / agent 网络]
    D --> E[沙盒检查通过]
    E --> F[CLI 复用或初始化]
    F --> G[admission / observer / prompt dispatch]
    G --> H[首个 assistant 文本或工具活动]
    H --> I[浏览器渲染]
```

这是因果顺序，不是把跨机器时间戳相减得到的一条精确时轴。`claim_rpc` 中的部分
处理与 DB 分配前的等待边界重叠，不能简单相加成端到端时间。

## 3. 埋点放在实际执行处

runner 记录外层调用区间；broker、manager、driver 返回子区间。记录开始、结束、
结果与缺失情况，保留嵌套关系。每项等待本身就是 wall duration 的一部分。

| 父阶段 | 子阶段 | 代码落点 |
| --- | --- | --- |
| `claim` | `db_claim`、`attempt_grant`、`repo_entitlement`、`clone_credential` | `api/routes.py:worker_claim()`；`storage/repositories/attempt_commands.py:claim_job()` |
| `reserve` | CP sandbox reservation | `runner._run_shared_live_attempt()` 的 `reserve_chat_sandbox()` |
| `acquire` | `supervisor_lock_wait`、`group_transition_wait`、`manifest`、`container_lookup` | `shared_session_supervisor.register_manifest()/acquire()`；在获取锁之前开始计时 |
| `acquire` | `setup_network_ensure`、`container_create`、`container_start`、`service_hosts_sync` | `chat_sandbox.ChatSandboxManager.create()/start()` 内部；区分 Docker 命令和外层验证 |
| `workspace` | `reset`、`git_clone_fetch`、`git_checkout`、`source_archive`、`archive_import`、`permissions` | runner 的 `_prepare_retained_checkout()` 及 manager 对应方法 |
| `workspace_resume` | `base_check`、可选 `base_fetch/bundle/import`、`head_read` | runner 的 native resume 分支和 `_import_retained_base()` |
| `setup` | `setup_script` | `setup.run_setup()`；记录 ran/skipped，禁止记录脚本、env 或 stdout |
| `runtime_check` | `version_check`、`binary_check` | `runtime_version_unavailable()`、`retained.has_binary()` |
| `agent_network` | `network_ensure`、`bootstrap_guest_stop`、`network_attach_detach`、`bootstrap_guest_start`、`service_hosts_sync`、`network_verify` | `ChatSandboxManager.transition_to_agent_network()`；复用走 `require_network_phase()` |
| `resident` | `reuse_check`、`residency_grant`、`member_lock_wait`、`old_process_close`、`member_state_prepare`、`cli_spawn`、`protocol_initialize` | runner、`_ensure_transport()`、`ResidentTransportFactory.open()`、driver `ensure_started()` |
| `dispatch` | `prompt_admission`、`process_census`、`observer_open`、`window_open`、`prompt_write`、可选 `prompt_ack` | runner admission；supervisor `start_turn()`；driver 实际发送处 |
| `control_receipts` | `mark_running`、terminal suspend/resume、`record_process`、context reporting、lifecycle event upload | runner 对应同步调用，避免把 CP 往返藏进无名间隔 |

`bootstrap_guest_stop/start` 与第一次 `container_start` 是不同 span，另记实际
`container_start_count`。若 Docker 命令耗时显著，再在专用 benchmark 主机上分析
daemon/containerd/Kata 内部边界；第一版不把一个 `docker start` 时长称为纯 VM boot。

每次调用有自己的 `span_id`。重试增加 ordinal，并为 retry backoff 单独记时，不能
覆盖第一次失败。setup 未配置是 `skipped`；成功但耗时不到展示精度才可显示接近零。
所有失败、超时和取消在 `finally` 关闭已打开的区间；进程被杀导致未闭合的区间标为
`incomplete`，结束时间和成功耗时为空。

## 4. 必须分开比较的启动路径

实际执行完后由动作分类，不能只靠 claim 时的 `context_status` 猜测。
**2026-09-14 已实现**：动作由 supervisor 在 lease/descriptor 中上报（§9.1）。

| `startup_path` | 定义 |
| --- | --- |
| `new_group` | 本次实际创建 container，并准备 workspace |
| `resume_stopped` | 原 container 已停止，本次启动它并恢复 native CLI |
| `resume_running` | container 在运行，本次重新打开 member CLI/native session |
| `reuse_live` | 原 container 与兼容 CLI 都复用，process generation 未变 |
| `other` / `unknown` | 动作不匹配，或失败/数据不足，保留事实，不强行分类 |

同时保留 `container_action=create|start|reuse|unknown`、
`resident_action=new|resume|reuse|unknown`、`workspace_action=prepare|reuse|base_import|unknown`，
以及 `member_origin=origin|fork`。Fork 创建本身是一项独立 operation；共享 group
的创建 span 不复制给每个 sibling。等待已有 group 准备的 member 计自己的等待 span，
通过 link 指向同一次创建。旧容器丢失按现有 LOST 规则失败，测量不引入重建恢复路径。

冷热缓存也不是一个布尔值：

- 镜像：本次开始前 digest 是否存在，是否实际发生获取；未知就记录未知。
- workspace：新准备、已有、补 base objects。
- dependencies：setup 是否运行；仅当 setup 有明确统计时报告 cache hit，否则未知。
- host page cache：生产默认 `uncontrolled`；实验记录测试环境及预热步骤。

每条摘要记录 deployment SHA、实际 sandbox image digest、CLI version、
sandbox runtime、execution profile、setup 是否存在、repo 基准版本、model/MCP 配置类别。
记录启动时最近的 host capacity 样本及年龄、slots used/total、同 group 活跃 member 数；
读取已有快照，不为了每次测量额外执行 `docker stats`。它们用于解释干扰，不冒充本次
启动独占的 CPU/内存消耗。精确资源归因是另外的测量问题。

## 5. 时钟与数据模型

### 本地耗时，跨服务关联

Python producer 用 `time.monotonic_ns()` 求差，存储相对 offset 和 duration（整数微秒），
API/UI 展示毫秒。它不受系统 wall-clock 跳变影响；单位为纳秒不意味着实际精度为纳秒。
wall clock 只用于按时间查询、关联和展示。[Python time 文档](https://docs.python.org/3/library/time.html#time.monotonic)

每个 CP/runner/broker 进程实例有独立 `clock_domain_id`。不直接相减不同 domain 的
monotonic 数值，尤其不能减 host 与 Kata guest 的时间。RPC client 的包围区间与
server 自己测得的处理区间都保留；二者差只能称为 transport/排队/调度余量，不能
直接称为网络延迟。跨 domain 子区间在 UI 中使用独立相对时轴，由父 RPC 关联，
不能画出未经证明的精确全局位置。

被选中操作的 DB 排队指标使用同一数据库的 wall clock，保存 `queued_since_at`、`queue_epoch`
与实际分配边界 `allocated_at`；每次重新入队更新周期。时间取自实际状态更新附近，
不把事务开始的 `NOW()` 默认值称为精确分配时刻。定义为状态决策边界，不是 commit
可见时刻；异常负差标记无效。旧记录最多提供明确标为 legacy estimate 的首次排队值，
不回填成 v1 精确记录。需要完整排队测量时在入队时就选择实验样本；开始 profiling
前已经入队的任务，其精确队列起点可缺失，不能从 claim 时回推。关闭时不为排队
profiling 新增时间戳写入。排队归因暂只给观察上下文，不仅凭低 host CPU 就判定轮询阻塞。

### 最小结构

一个选中的 attempt 一份摘要，保存有界 span fragments；CP 在分配 attempt 时生成关联 ID。
每个新 attempt 都是新样本，同一 job 的重试通过 job id 关联。

```text
attempt_startup_measurements
  attempt_id (PK), job_id, profiling_run_id, measurement_id, schema_version
  host_id, sandbox_id?, sandbox_generation?, activation_generation?
  process_generation_before?, process_generation_after?
  queue_epoch, startup_path, container_action, resident_action, workspace_action
  runtime_id, runtime_version, sandbox_runtime, image_digest, deployment_sha
  readiness_source, cache_facts, host_capacity_reference, benchmark_metadata?
  milestones, durations, producer_fragments
  startup_outcome: dispatched | failed | cancelled | lease_lost | incomplete
  failure_stage?, error_class?, measurement_complete, dropped_spans
  observed_at, received_at

producer_fragment
  producer: control_plane | runner | broker
  producer_instance_id, clock_domain_id, report_seq
  fragment_state: open | final
  spans: [{ span_id, parent_span_id?, linked_span_id?, name, ordinal,
            start_offset_us, duration_us?, outcome, bounded_attributes }]
  milestones: [{ name, offset_us, source }]
```

`measurement_complete` 表示该路径要求的启动边界和 producer 摘要齐全，不要求被
明确跳过的步骤存在测量值。`startup_outcome=dispatched` 不等于任务最后成功；模型
执行失败不能回写成“沙盒启动失败”。首个输出可能很晚，在独立的 post-dispatch
观察片段中追加，不阻碍启动摘要完成。没有输出时保留 null 和 observation outcome。

每个 attempt 初始上限建议 128 个 spans、64 KiB JSON；优先保留摘要、关键 milestone
和失败区间，超限标记截断。重复 Docker inspect 可聚合为 count/total/max，不记录
任意参数。上限在试运行时验证，不作为当前已测开销或存储量的宣称。

低基数聚合维度仅使用 stage、path、runtime、execution profile、outcome 等枚举。
job、repo、owner、host、digest 等可用于受权明细过滤，但不做所有维度的笛卡尔积。
不记录 prompt、token、环境变量、命令行或原始错误文本；身份关联来自认证上下文。

Span 的父子关系和状态采用可映射到 OpenTelemetry 的模型，第一版仍可只使用
标准库 collector。以后接 exporter 时不重定义测量边界。
[OpenTelemetry Span API](https://opentelemetry.io/docs/specs/otel/trace/api/#span)

## 6. 采集与可靠性

以下步骤仅在 profiling run 有效且根操作被选中时执行。默认关闭或未选中时直接走
现有业务路径，不创建 collector、关联 ID、timing fragment、measurement frame、
exporter 或测量存储行；也不做计时后再丢弃。根操作选择结果传给子阶段，重复请求不重抽样。

1. CP 在选中的 claim 分配后建立 measurement；claim 凭据阶段失败也记录 CP 侧结果。
   claim 失败并撤回/删除 attempt 的现有路径，另留以 `claim_operation_id` 关联的
   有界诊断记录，不能制造成功启动样本或依赖仍存在的 attempt 外键。
2. runner 持有轻量 collector，在 claim 返回后立即开始；启动过程内的计时只写内存。
   runner→broker 请求透传 measurement/span correlation，不把 host credential 放入 guest。
3. broker 的 acquire/resident 响应和错误响应附带有界 timing fragment。
   `workspace_broker.run_shared_turn()` 当前先等待 `start_turn()` 返回再开始 stream：
   实现时要在 driver 实际 prompt write 处记录 milestone，不能把首个 runtime event
   当成 dispatch。broker 用独立可排队的 measurement frame 及时传给 runner；即使
   driver 还在等 turn ack，也不等到模型输出才发送。stream 生命周期仍由原有 admission
   和错误规则控制；measurement frame 本身不证明任务被接纳。
4. 在 claim、sandbox-ready、dispatch 或提前失败时，非阻塞排入 exporter 队列，输出
   有界结构化 host 日志，并批量上传启动 endpoint。阶段内不增加逐 span HTTP POST。
   CP 独立片段自己入库；不要让并发更新整份 JSON 丢掉另一个 producer 的数据。
5. 新 worker 写 endpoint 沿用现有 attempt lease fence，只能更新自己的 timing。
   `(attempt_id, producer_instance_id, report_seq)` 保证重传幂等，旧 seq 不能覆盖新 seq，
   final 片段不能退回 open。内存队列、重试次数和上传 deadline 都有上限。
6. 仅对开启的采集，在 finish 前允许有严格上限的 best-effort flush；失败不会阻止 finish、改变任务结果
   或延长 lease。lease 丢失或 finish 后的迟到写仍拒绝，残片保留在 host 日志。
   v1 不开放绕过 fence 的历史补写；CP 将选中但缺失的记录标为 partial/incomplete。
   失败率与覆盖率的分母来自本 run 选中的实际 attempt/排队记录，不能只数成功上传的
   measurement；关闭/未选中的操作不当成丢失样本，也不新增全站 profiling 统计。
7. 旧 broker/runner 的缺失字段降级为 unavailable。新 frame 需要 reader 显式支持、
   capability/version 协商及滚动升级测试，不能假定未知 frame 在旧 reader 中安全。

不经过 guest 的第三个网络出口，不引入 SDK credential 到 sandbox，不改变 Kata、
网络阶段、member 隔离或凭据边界。测量代码自身失败不成为任务失败条件。

建议详细数据保留 30 天，后续按日保存有界分布统计；这是待容量验证的初值。
按 `attempt_id` 取明细、按时间/运行路径取摘要，沿用 Admin authorization 和 metadata
allowlist。默认不采集。专用 benchmark 在显式指定的 run 内可选择 100% 采样；其他实验
可配置比例，选择在根操作开始时完成。报告必须显示 run 范围、有效期、选择率及采集状态。
run 到期、关闭或耗尽上限后按 §0 停止采集；报告保留为历史实验，不暗示持续监测。

## 7. 如何用数据定位瓶颈

Admin Runs 的 attempt 明细对采集过的实验显示 Startup 区域；未开启时显示 profiling
未启用，不呈现错误或伪造零值。另提供相同数据的 JSONL/CSV 导出，
让第一轮 benchmark 不依赖 UI 开发完成。

- 顶部：启动路径、沙盒准备、发送 prompt、readiness source、startup outcome、覆盖率。
- 时间线：runner 外层各阶段、可展开的 broker 子阶段、失败/重试/未闭合阶段。
  跨时钟域单独显示来源；没有测到的步骤不画成零长度。
- 汇总：每个 cohort 的样本数、成功/失败/取消/未完成数、P50/P95、慢样本链接和版本。
  足够样本后才解释 P99；始终显示样本量。
- 尾部分析：取同一 cohort 中主指标最慢的 5% attempts，再看这些 attempts 的阶段分布。
  不把各阶段各自的 P95 相加，也不把“总 P95 − 某阶段 P95”当成优化收益。

同一时钟域内，父区间的 self time 为父耗时减去子区间的**时间并集**；并行子区间
不能简单求和。跨时钟域仅展示 RPC 包围关系和余量。串行顶层阶段应能解释主指标，
剩余时间显示为 `unattributed`，不偷偷归给 runtime 或网络。

优化优先级依据“慢样本中关键路径耗时 × 出现频率”，而不是哪个操作名字听起来昂贵。
观察到 clone 慢，才细看 fetch/checkout/archive/import；观察到 acquire 在并发下慢，
才分析锁等待；观察到网络切换慢，先区分 stop、重接网络、start 和 hosts sync。

## 8. 可重复的 benchmark 方法

### 三层实验

| 层 | 内容 | 用途 |
| --- | --- | --- |
| A：基础设施 | 固定 digest、无业务仓库/无 setup，通过真实 manager 创建、启动、切换网络、执行固定 probe | 隔离 Docker/Kata/network 成本；单独命名 `infrastructure_benchmark`，不冒充完整任务启动 |
| B：平台完整启动 | 固定 repo commit、setup、runtime/model/MCP；通过正常 claim→broker→driver 到 dispatch | 主回归基准，包含真实 grant、源代码准备和 observer 开销 |
| C：用户体验 | 相同固定小任务，通过正常请求到首个可见活动/文本 | 验证启动变快是否实际改善等待；模型和外部服务波动单列 |

A 使用真实隔离与网络约束；B/C 不绕过认证、admission 或 observer。fake transport
只用于验证计时正确性，不把它测出的结果称为真实 CLI 启动性能。

### 场景与环境

首批至少覆盖四种路径，主矩阵在与生产相同的 Kata runtime 上运行；trusted Docker
另成 cohort。每种路径先测无 setup 的小 repo，再测一个固定 commit、锁定依赖的代表
repo。并发分别取 1、约半容量、满配置容量；跨 group 并发与同 group sibling 并发分开。

固定并记录 host 规格、Docker/Kata 版本、guest kernel、CPU/内存限额、deployment SHA、
镜像 digest、CLI version、repo commit、setup 配置、model、MCP 集合类别、并发数、
host 负载、profiling 开关/run 配置、已有 resource collector 状态和实验顺序。
`new_group` 默认要求镜像已存在；镜像缺失的 host provision
实验单列。生产环境不清 page cache，不删除生产容器来模拟冷启动。

使用专用、可丢弃的 benchmark group 及固定测试 repo。每次 `new_group` 用新 group；
`resume_stopped` 先完成规定的 warm-up turn、通过既有安全 idle-stop 流程停止，再确认
原 container 存在；`resume_running` 在测试设施内停止该 member transport 后恢复同一
native session；`reuse_live` 必须校验 process generation 未变。其他任务和终端不能
参与这些 group。没有达到前置状态的试验记为 preparation failure，不冒充目标路径。

不要在同一 chat 连续发送 200 次并宣称 200 次同条件 warm start：native context 会增长。
每个样本先用同样步骤建立等长上下文，再只测指定的一轮；保留初始化阶段的独立记录。
不能依赖平台不存在的 transcript replay 或 checkpoint restore 来重置实验。

### 样本与比较

1. 先每个关键 cohort 跑 20 次 pilot，检查边界、前置状态和缺失率；pilot 不与正式结果混合。
2. 正式 A/B 初始建议每个关键 cohort、每个版本至少 200 次，报告 P50/P95、分布与
   bootstrap 95% CI。200 次的尾部仍有限，CI 不稳定就继续采集；不能将这个数当作
   所有回归都能检出的保证。P99 在至少约 1000 个样本前只作为探索值。
3. 在同类 host 内按时间 block 随机交替 baseline/candidate；每次仅改变一个优化。
   涉及 image 的变更分别预取对应 digest。无法交替的 host 配置变更用匹配主机与交叉
   时间窗口，并显式报告残余干扰。CI 按 host/time block 重采样，避免把相关样本视为独立。
4. 保存每次结果，包括失败、超时、取消、准备失败和未上报；只有成功达到相应 milestone
   的样本进入该 latency 分布，并同时展示该里程碑的失败率/覆盖率。timeout 不丢弃、
   不按 timeout deadline 填成成功耗时。所有队列等待都另列，未领取任务不消失。
5. 比较同 cohort 的绝对变化（ms）与相对变化（%）；profiling 阶段对比解释耗时来源，
   实际优化收益由两个版本都关闭 profiler 的外部完整路径 benchmark 验证。同时看
   主指标与目标阶段是否一致改善，
   并检查失败率、上下文保持、网络隔离、资源压力和其他路径有无回归。
6. 阈值在 baseline 后、看 candidate 结果之前确定。例如目标可写成“某 cohort 的
   P95 降低至少 X ms，启动失败率不超过预先约定的容忍差”；X 来自基线和用户等待目标。
   现在不承诺任意的 1 秒/5 秒 SLO，也不把一次最快值作为结论。

benchmark 报告至少包含：环境 manifest、实验顺序、全部 attempt ID、每个 cohort 的
样本/失败/缺失数量、分布及 CI、阶段解释、优化前后差值、限制条件。原始结果应可重新
聚合得到同一报告。测试数据不携带真实用户 prompt 或服务凭据。

## 9. 实施顺序与验收

按可独立验收的操作组织交付；下列是实现依赖，不强制逐项拆 PR。新增工作从 dev
分支开展，PR 目标为 dev；既有 #258 的后续修订保持在该 PR：

1. **启动测量切片（已实现）**：默认关闭的快路径、run、runner/broker 阶段、四路径动作、
   call ID 片段协议、有界导出与持久化；见 §9.1。
2. **文件/终端服务端切片（已实现）**：复用通用采集器并接入 CP/broker；浏览器
   点击与可见/可交互终点仍按平台清单 §3–§5 延期。
3. **真实 benchmark 与基线报告（已实现）**：`scripts/bench_operation_overhead.py` 用真实
   HTTP + 真实 Postgres 跑文件与终端的六个服务端操作，`--startup` 段用真实 Docker 容器
   产生四条启动路径；固定 fixture、报告 n/p50/p95、增量、丢弃数与后台成本，并按
   `max(10 ms, baseline p95 × 1%)` 逐操作给出结论。结果见
   [服务端验收报告](../../acceptance/2026-09-18-operation-measurement-server-side.md) §3.3。
   仍未纳入该实验的：真实 agent CLI 初始化、生产 Kata 隔离、跨 replica。
4. **持久化和 Admin 读面（已实现）**：#218 的能力已改为消费统一 schema v3 记录，
   含实验目录、样本列表/详情、分布与本地 writer 丢弃计数。

### 9.1 已交付的切片

`apps/backend/cloud_agent_shared/operation_profiling.py` 实现开关、run descriptor 和纯计时 collector；
`runner.run_once()` 在 claim 前开始一次 `attempt.startup` 测量，`_run_shared_live_attempt()`
用现有的 `stage` 变量逐阶段打点，`_stream_shared_agent_turn()` 在 prompt 交给 supervisor
时关闭测量。已落地：

- `AGENT_OPERATION_PROFILING_ENABLED`、`AGENT_OPERATION_PROFILING_RUN`、
  `AGENT_OPERATION_PROFILING_SINK` 三个变量（见 [ENVIRONMENT.md](../../ENVIRONMENT.md)）。
- 单调时钟跨度、父子 span、阶段时长（由相邻打点推导）、失败阶段、`dropped_spans` 上限，
  以及 `dispatched`/`failed`/`cancelled`/`lease_lost`/`incomplete` 结果。记录含
  `duration_us` 总耗时；最后一个阶段由 `finish()` 的收尾边界关闭，所以**失败或仍在
  进行的那一段也有时长**，不会只剩一个没有耗时的 milestone。
- attempt/job/thread/sandbox/runtime 关联，`context_status`、`workspace_action`；
  结构化日志、可选 JSONL 与 Postgres/Admin。

**部署使用的父子进程路径也保持同一条记录。** 长驻 host 在 claim 前选择并计时
`claim` span，经 `_profiling` handoff 将原 measurement id、起点、已完成 span 和真正
消费预算的 admission instance 交给 turn child；spawn 只剥离其他父进程私有字段。
child 不重新抽样、不重新认领预算，并在 prompt dispatch 冻结后直接投递同一缓存记录，
不依赖 JSONL，也不在读取模型输出前等待网络。

**四条启动路径按实际动作分类。** supervisor 在 lease 里返回本次 activation 对容器的
动作（`create`/`start`/`reuse`/`unknown`）和 member CLI 的动作
（`new`/`resume`/`reuse`/`unknown`），runner 据此得出 `startup_path`：

| container_action | resident_action | `startup_path` |
| --- | --- | --- |
| `create` | 任意 | `new_group` |
| `start` | 任意 | `resume_stopped` |
| `reuse` | `reuse` | `reuse_live` |
| `reuse` | `new` / `resume` | `resume_running` |
| 其他 | 其他 | `unknown` |

动作由真正执行它的代码上报，不是事后从容器状态推断：先停后启的容器和从未停过的容器
在跑起来之后完全一样。`create` 优先于紧随其后的 `start`，否则“这次付了创建代价”会被
记成“这次发现它是停的”。动作**不依赖开关**——它是事实不是测量，所以即使 broker 没开
profiling，runner 仍能正确分类路径。反过来，动作缺失时不猜：`reuse` + 未上报的
resident 动作返回 `unknown`，而不是默认成 `resume_running`。

**关键内部阶段。** 跨进程的时长以 duration 形式挂在它所属的本地阶段下：

- broker 侧经 lease/descriptor 返回：`container_create`、`container_start`、
  `phase_network`、`service_hosts_sync`、`cli_spawn`。
- runner 侧直接记入测量：`agent_network` 阶段的 `network_ensure`、
  `bootstrap_guest_stop`、`network_attach_detach`、`bootstrap_guest_start`、
  `service_hosts_sync`。

子阶段带 `source`，不带 `start_offset_us`：另一个进程的单调时钟与本进程不可比，
给它一个本地偏移会显得精确但毫无意义。

**broker 继承本次选择，不自行决定。** broker 一次只看得到一个调用，不知道它属于哪个
attempt，所以无法自己判断该操作是否被选中。runner 把选中的测量上下文（run id、
operation、以及**绝对墙钟**截止时间——两个进程没有共同的单调时钟）随请求发出，broker
再用自己的开关、自己的 run descriptor 和继承的截止时间校验后才记录。因此 runner 已因
过期停止后，broker 不会继续测量；过期、错 run、非选中 operation 一律拒绝，而容器动作
照常上报。

**测量配置不能让业务请求失败。** 该字段在 HTTP 模型上刻意不做类型与长度校验：任何
畸形内容都只在 `start_stage_recording` 里被判为“不记录”，绝不以 422 拒绝一次 acquire。
`run_id` 的长度上限在**读取 descriptor 时**统一执行（`MAX_RUN_ID_CHARS`），超限表现为
profiling 关闭并记一条 warning，而不是每次启动失败。acquire 与 resident 两个接口
一致，畸形上下文测试覆盖两者。

记录用 `producers` 列出实际贡献者。**片段 = 一次调用**，协议要同时表达三件事：

| 问题 | 字段 |
| --- | --- |
| 这是哪次调用 | 请求与响应各带 `call_id`；runner 在调用前铸造并登记期望 |
| 是否完成采集 | 响应带 `profiling_recorded`（建过 recorder）与 `profiling_truncated`（因自身截止时间提前停止） |
| 是否正常跳过 | `profiling_recorded=true`、`profiling_truncated=false` 且 `stages=[]`，才是“本次无需执行该阶段” |

因此完整性按 **call id** 判断，而不是按阶段名或生产者：重试是**第二个片段**，必须各自
交代，不能用后一次成功掩盖前一次；而正常复用 CLI 时 `cli_spawn` 本就不存在，
`resident_action=reuse` + `stages=[]` + `profiling_recorded=true` 是**完整**片段，
不是缺失。`missing_fragments` 形如 `["broker:c2:resident_start"]`，
`measurement_complete=false`；broker 没开时两个调用都缺席。子阶段也带 `call_id`，
所以展开重试时能看出哪段属于哪次尝试。

**空 `stages` 有两种含义，必须区分。** broker 的本地期限可能比 runner 的短：等待成员锁
就把期限耗光，之后每个子阶段都被跳过，`stages=[]` 且没有任何截断痕迹。因此 recorder
分别统计“被截止点截断”和“因已过期而根本没开始”，响应带 `profiling_truncated`。
此时片段**已应答但被截断**：它测到的阶段照常保留，根记录标 `truncated=true` 与
`truncated_fragments=["broker:c2"]`，`measurement_complete=false`——既不算缺失，也
不算完整。

**截断状态跨进程保留。** broker 在自己的截止点截断的子阶段带 `truncated=true` 回到
runner，并置根记录 `truncated=true`；`measurement_complete` 要求边界齐全、**未被截断**、
且没有缺失片段。一个被截断的区间是下界而不是真实耗时，所以不能算完整样本。

三条开销与边界约束按 §0 的要求落实：

- **发布不占 attempt 线程，日志也一样。** 日志行与 JSONL 写入同走一条有界队列，由一条
  仅在开启且产生记录后才启动的后台线程处理；队列满时丢弃并计数。日志不是例外：一个
  阻塞的 handler（慢 socket、写满的盘、同步 shipper）和慢文件系统一样，会把延迟算进
  被测量的启动时间里。exporter 线程**启动失败**（线程上限、fork 状态异常、沙箱拒绝
  clone）不向业务抛异常：该进程改用永久禁用的 exporter，丢弃并计数测量，启动照常继续
  ——这段代码执行时 prompt 往往已经发出。
- **预算在确认选中后才消耗。** `start_operation` 只是预留名额，`discard()` 归还未选中
  的名额（空 claim、目标不匹配、claim 抛异常），`finish()` 才提交。否则一次空轮询就能
  吃掉 `max_operations: 1` 的实验。
- **目标过滤要求全部匹配。** `target` 只接受 `host`/`job_id`/`thread_id`/`sandbox_id`，
  未知字段在读取 descriptor 时就拒绝；`bind()` 要求每个配置字段都存在且相等，**缺失值
  算不匹配**（`sandbox_id=None` 不会通过 `sandbox_id=wanted`），且带 target 的 run 不会
  上报一次从未绑定过的测量。
- **到期即停止采集，且截断一致。** 截止时间在采集过程中生效：到期后不再记录
  mark/span/attribute；**跨过截止点的 span 与子阶段同样在截止点截断**并标
  `truncated=true`，不会出现子区间比父区间还长的记录。根记录在截止点收尾而不是在发现
  时刻收尾，并标 `measurement_complete=false` 与 `profiling_stopped=expired`；
  已采到的部分照常保留。

### 9.2 自身开销实测

`scripts/bench_startup_profiling_overhead.py` 用真实的调用序列（1×`start_operation`、
1×`span`、12×`mark`、8 个 attribute、1×`finish`）对比五个 cohort，n=20000/档，
CPython 3.12.13 / macOS arm64：

| cohort | p50 | p95 | 相对 `bare` 的均值 |
| --- | --- | --- | --- |
| `bare`（无埋点） | 0.334 | 0.416 | — |
| `off`（开关关闭） | 0.709 | 0.917 | **+0.39 µs** |
| `on`（开启、无 sink） | 25.08 | 29.46 | **+25.8 µs** |
| `onsink`（开启 + JSONL） | 23.92 | 29.04 | **+24.5 µs** |
| `onlog`（开启 + 每记录 1 ms 的 handler） | 26.29 | 29.46 | **+26.0 µs** |

结论与边界：

- **关闭不是字面意义的零开销**，而是约 18 次 no-op 调用 ≈ 0.4 µs/attempt；且
  `runner.py` 把唯一一次多属性 `set_attributes` 放在 `if measurement is not None:` 里，
  实际关闭路径比 `off` cohort 还略低。相对真实启动（§8 中为毫秒到秒级）可忽略，
  但不声称“绝对零成本”。
- **开启后约 +30 µs/attempt**（2026-09-18，5,000 次复跑），主要来自构造记录与
  `json.dumps`，不是取时钟或打点。
  远低于 §9 验收建议的“P95 增量不超过 10 ms 或基线 1%（取较大者）”。
- **日志 handler 不再落在 attempt 线程上。** `onlog` 用每记录 1 ms 的 handler，
  与 `on` 的差值在噪声内（p50 +1.2 µs、p95 +0.001 µs，重复运行符号会翻转）；
  若格式化仍同步，仅这一项就会给每次 attempt 加上 ≥1000 µs。
- 本基准测的是**埋点自身的开销**，不是端到端启动：没有 claim、容器、supervisor、
  CLI 或网络。合成速率（约 40k 记录/秒）下 exporter 会丢弃大部分记录，这是预期且有计数
  的；真实启动一次只产生一条记录。
- **broker 侧未纳入本基准，已由业务实验覆盖**：`scripts/bench_operation_overhead.py`
  在真实 HTTP 路由后接真实 collector 与真实 Postgres（broker 用真实片段协议的进程内替身），
  六个服务端操作在 `on-local`/`on-persist` 下的 Δp95 都远在 `max(10 ms, off_p95 × 1%)`
  预算内，两次独立运行都通过。结论不能更细：同一 `off` cohort 自己的 p95 在两次运行间
  就摆动 2.9 倍，任何小于该摆动的逐操作增量都不能归因于埋点；第一次运行里
  `terminal.list` 的 `on-local` 曾出现 p95 +3.46 ms，第二次未能复现（−3.13 ms），
  记为运行间波动。四条启动路径在同一脚本的 `--startup` 段用真实 Docker 容器产生
  （`create`/`start`/`reuse`/`reuse` 计数非零），n=5/6 时 p95 就是最大值而不是尾部估计，
  真实容器动作是数百毫秒级。详见[服务端验收报告](../../acceptance/2026-09-18-operation-measurement-server-side.md) §3.3。

与目标形态的差距，后续切片补齐：

- **选择发生在 runner，不在 CP。** 因此 `max_operations` 是单进程预算，不是跨 replica 配额；
  服务重启不续期这一条由 run descriptor 的绝对 `expires_at` 保证。broker 侧只校验开关与
  有效期，不参与名额分配。
- **持久化和 Admin 已实现**：启动记录不依赖 JSONL，按确切历史 attempt 归属并可从
  Admin 的实验目录、列表、详情和分布读取。
- **仍有阶段在更内层**：`setup_script` 的脚本内部分解、`readiness_source` 的 runtime
  差异（Claude 的 process-alive 与 Codex 的 protocol ack），以及 `prompt_ack`。
- **没有 post-dispatch 片段**（首输出）。

计时正确性的验收使用可控时钟和固定延迟 fake，性能结论使用真实运行：

- 人为给 clone/network/CLI init/observer 各注入已知延迟，只有对应阶段及包含它的父区间增加。
- Kata 新建记录 bootstrap stop 和第二次 start；resume/reuse 没有执行的阶段显示 skipped。
- native resume 与同 PID 复用可区分；Claude 的 process-alive 不被渲染为 protocol ack。
- 墙钟跳变、不同 producer 起点、并行 span、重试与 backoff 不产生负时长或双重累加。
- setup 失败、claim 凭据失败、CLI 启动失败、取消、timeout、lease loss 和 broker/runner
  被杀，均保留正确结果或明确缺失；缺失记录仍计入覆盖率分母。
- 无模型输出时也及时收到 dispatch measurement；runtime 历史/system 事件不能触发首输出。
- 重传不翻倍；并发 producer 不覆盖；旧 seq/fenced writer 不能修改最终结果或生命周期。
- exporter 不可用、队列满、payload 截断时任务正常结束，数据标 partial；常规日志无敏感内容。
- 验证默认关闭时没有 profiler 初始化、计时/属性分配、后台线程/timer、新增命令、
  profiling 上报或 DB 写入；相对无 instrumentation 的版本检查 off 路径回归。
- 同版本的 off/on A/B 验证 profiling 开销，初始验收建议开启后新增启动 P95 不超过
  10 ms 或基线的 1%（取较大者）；这是待测预算，需报告吞吐、资源/上传开销与 CI。
  若超出就缩小采集范围、降低密度或留在专用测试环境，不能宣称没有 observer effect。
- 到期/上限/主动关闭终止采集且不取消业务；重启不续期；跨 replica 选择不翻倍，
  过期/未授权的 client header 不能打开 profiler。未开启与选中后的数据丢失可区分。
- 每个实施 PR 跑 `make check`；新增 DB 行为跑 `make test-db`；实际 Kata/driver 路径跑
  外部集成测试并保留结果，不能用单测替代真实启动验证。

### 9.3 已交付的文件与终端服务端切片

文件/终端的**服务端**接入已实现，前端终点尚未接入。交付内容是：
`cloud_agent/operation_measurement.py` 做 CP 侧根操作选择，
`apps/backend/cloud_agent_host/operation_call.py` 做 broker 侧片段校验与应答，
`apps/backend/cloud_agent_shared/operation_profiling.py` 通用化后可被不同 producer 调用。

| 能力 | 落地位置 |
| --- | --- |
| schema v3 | `outcome` 为通用结果字段；带 boundary/coverage/完整性；`startup_outcome` 只对 `attempt.startup` 作为别名保留 |
| 通用 producer | 记录带 `producer`（根操作归属）与 `producers`（应答过调用的生产者）；`timing_sources` 只列真正进入时间线的来源 |
| CP 根操作选择 | 目标匹配走 collector 的 `bind()`：路由无法证明的 fact 不匹配，不匹配者归还名额 |
| 名额 | 仍为单进程预算，由 CP 根操作消耗；broker 子调用不消耗。跨 replica 分配仍未实现 |
| 交互关联 | `X-Agent-Interaction-Id` 关联交互；list/create/stream 各自有唯一 measurement_id |
| 选择缓存 | 单进程有界（`MAX_SELECTIONS`）+ TTL（`SELECTION_TTL_S`），并受 run 截止时间约束 |
| broker 阶段 | `files`：`admission`/`network_phase`/`exec`；`terminals`：`admission`/`network_phase`/`pty_spawn`/`list`/`stream_attach` |
| 流式应答 | 终端 SSE 的 body 是终端字节，片段的 `call_id`/`recorded`/`stages` 走响应头回传 |
| 关闭路径不变 | 未选中时响应体与响应头与接入前逐字节一致（`profiling` 键不出现），不新增请求或监听 |

**明确未交付。** 浏览器终点（点击、UI commit、xterm 可交互、本地导出与预领名额）、
纯缓存命中不产生 HTTP 请求时的覆盖、跨 replica 的统一名额和三类线上操作基线
仍未实现。因此当前只能声明 **server-only** 覆盖：不能把服务端耗时
说成“点击到可用”，也不能用文件 helper 微基准代替 CP → broker → 前端整段采集的开销实测。
文件与终端的入参、成功终点、缓存/重试/就绪边界和验收矩阵以
[平台操作测量清单 §3–§5](2026-09-14-platform-operation-measurement-inventory.zh-CN.md) 为准。

## 10. 代码与现有设计索引

- [runner](../../apps/backend/cloud_agent_host/runner.py)：claim、准备、resident、admission、stream。
- [chat sandbox](../../apps/backend/cloud_agent_host/chat_sandbox.py)：容器及两阶段网络，Kata bootstrap restart。
- [shared supervisor](../../apps/backend/cloud_agent_host/shared_session_supervisor.py)：group/member、锁和 turn 边界。
- [resident transport](../../apps/backend/cloud_agent_host/resident_transport.py) / [drivers](../../apps/backend/cloud_agent_host/drivers.py)：实际 CLI readiness 与 prompt 协议。
- [workspace broker](../../apps/backend/cloud_agent_host/workspace_broker.py)：RPC 和流式 frame。
- [operation profiling](../../apps/backend/cloud_agent_shared/operation_profiling.py)：开关、run、collector、片段协议。
- [operation call](../../apps/backend/cloud_agent_host/operation_call.py)：broker 侧一次调用的片段校验与应答。
- [operation measurement](../../cloud_agent/operation_measurement.py)：CP 侧根操作选择、名额与选择缓存。
- [worker API](../../cloud_agent/api/worker.py) / [workspace API](../../cloud_agent/api/workspace.py)：启动入库与文件/终端根操作。
- [measurement writer](../../cloud_agent/measurement_writer.py)：CP 有界异步持久化。
- [host capacity](../../apps/backend/cloud_agent_host/host_capacity.py)：已有 host 快照。
- [Admin observability](2026-09-10-admin-observability-v1.md)：接收时间、权限、metadata 语义。
- [native session design](2026-08-31-native-session-fork-shared-workspace-design.md)：保留容器、native resume 与共享 workspace 边界。
