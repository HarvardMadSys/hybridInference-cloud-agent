# 平台操作测量清单

日期：2026-09-14。状态：设计清单；**§0 的开关与 runner 侧启动计时已实现**，
统一采集、报表与其他操作尚未实现。
基于本地代码检查，扩展 [沙盒启动测量设计](2026-09-14-sandbox-startup-measurement-design.zh-CN.md)。

目标：在按需开启的 profiling 实验中，每个主要用户操作都能回答：
**多久可用、是否成功、卡在哪一步、结果是否新鲜**。
执行中的 CPU、内存和 I/O 继续由已有 member-window/eBPF 体系解释；平台操作的等待由
前端和服务端阶段计时解释。每种测量都保留来源、范围和覆盖率。

**2026-09-14 范围修订：新增平台 profiling 默认关闭，优先服务前期性能定位、基线和优化
对比，不作为长期常驻采集。** 下文所有计时、operation ID、spans、报表和上报，仅针对
显式开启且选中的实验操作。此决定同时取代启动设计中原先的默认全量采集建议。

## 0. 可选 profiling 开关

### 两个状态，默认关闭

| 状态 | 行为 |
| --- | --- |
| `off`（默认） | 走原有业务路径，不初始化新增 profiler、分配 measurement ID、取计时值、构造 span/attributes、序列化或上报 profiling 数据 |
| `profiling` | 仅对本次实验选中的目标和操作计时；到截止时间或样本上限后结束 |

部署级主开关为 `AGENT_OPERATION_PROFILING_ENABLED`，缺省为 `false`。
**2026-09-14 已实现**（`cloud_agent_host/operation_profiling.py`，目前接入 runner 的
`attempt.startup`；变量说明见 [ENVIRONMENT.md](../../ENVIRONMENT.md)）。仅设为 `true`
不代表所有请求自动被采集；还需配置有明确范围和截止时间的 profiling run
（`AGENT_OPERATION_PROFILING_RUN`，未配置或不合法时开关不生效并只记一条 warning）。
第一版通过测试/运维配置提供 run descriptor，
不要求先开发设置页面或完整的动态控制平台。主开关变更若需要重启，必须在实验准备
阶段完成，不能把重启后的冷缓存当成开关本身的开销。

实现的边界：选择发生在 runner 根操作处，所以 `max_operations` 目前是**单进程**预算，
不是清单要求的跨 replica 统一分配；产出写入结构化日志，并在配置
`AGENT_OPERATION_PROFILING_SINK` 时追加 JSONL，尚无 CP 持久化与 Admin 读面。
截止时间由 run descriptor 的绝对 `expires_at` 在首次读取时转成单调期限，服务重启
不会续期。

一个 profiling run 的设计示例（不是可直接执行的现有命令）：

```yaml
mode: profiling
run_id: terminal-open-baseline-01
target: {sandbox_id: "<专用测试沙盒>"}
operations: [terminal.create, terminal.reattach]
expires_at: "<开启时生成的固定 UTC 截止时间，例如 15 分钟后>"
max_operations: 200
sample_rate: 1.0
```

可按测试 session/thread/sandbox 或指定 host 限定目标；host 级实验也必须显式选择，
不隐式扩大到全部用户。`operations` 选择根操作，其必要子阶段自动继承。缺少范围、
有效截止时间或合法上限的 run 不生效，业务照常执行。

截止时间是 run 的固定属性，服务重启、重试或刷新页面都不能续期；运行时用本地剩余
时长/单调时钟落实期限。达到样本上限后不接受新根操作，已选中的操作可在原截止时间
及各自测量窗口内完成。到期或手动关闭时，尚未完成的采集在有限收尾期限内结束并标
`profiling_stopped`/partial，不中断真实任务、终端或文件请求。长连接不会让实验无限
延长，也不会为了“完成 trace”保留后台采样。

样本上限约束被选中的根操作，child spans 另有 count/bytes 上限。开启期间由可信的
run admission 统一分配根操作名额，不能让每个 replica 各采 200 次后声称总共 200 次。
同一次 interaction 的重试共享选择结果，并报告物理请求次数。采样决定发生在 root
入口；不能先做完整采集，结束时再扔掉大部分数据。

### 关闭时的执行成本约束

- 默认入口只做廉价的关闭状态判断；跳过整套 profiling 路径。不能每个 span 都解析
  环境变量、查询 DB、读取配置文件，或分配 context manager 后再判断关闭。
- 不启动 profiling exporter 线程、周期任务、采样 timer，不执行额外 Docker/guest
  命令，不生成 profiling HTTP 请求、日志或 DB 行。已有业务请求和状态记录继续原样工作。
- 前端关闭时不加载 profiler 模块，不注册 profiling 专用 PerformanceObserver、
  输出/按键监听器、timer 或 beacon；业务原有的 terminal/file 监听器照常运行。
- 前端可用普通页面/session 配置中携带的实验描述开始测量，不为 off 状态新增配置轮询。
  浏览器传来的开关/header 不能直接让 CP 或 broker 启动采集，服务端独立校验 run 和范围。
- 服务间透传已授权的 run/operation 选择结果；各自再次校验有效期/本地开关，拒绝过期
  或未知实验。任何 producer 未开启都记为局部覆盖，不能显示完整端到端 trace。
- 关掉主开关仅能承诺**新增 profiling 路径的开销接近零**，具体由验证确认，不能宣称
  整个平台没有观测开销。现有 eBPF、member resource sampler、host capacity、必要
  heartbeat/audit/生命周期记录有各自职责，不由该开关一并关闭。

Latency profiling 不依赖额外开启 eBPF 或加密集 `/proc`/Docker 采样。已有 resource
采集的启用状态和采样频率写入实验 manifest，baseline/candidate 保持一致；如果以后
需要按需切换这些已有 collector，应独立设计生命周期和安全约束，不能只关闭上报。
初版不增加新的高成本 profiler；后续深入分析只在显式选择的测试目标上开启。

### 实验数据的含义

- 区分 `not_enabled`、`not_selected` 和选中后的 partial/failed；前两种不是采集故障，
  不为每个未选中的请求写一条空测量记录。
- 覆盖率分母是 run 中被选中的操作；选择率另报。实验 cohort 的失败率不能冒充全站
  失败率，关闭时不为了这个报表新增全站计数。
- 实验结束后历史报告仍可查看，但标注 run 起止、选择范围和版本；不继续后台刷新采集。
- 第一版的目标是可重复的性能实验和离线报告；长期 SLO、常驻指标和告警需要另外决定。

### 自身开销也要测

外部 benchmark harness 始终用相同方式计时，至少对比：

1. 无新增 profiling 代码的基线版本 vs 新代码处于 `off`，检查关闭路径回归。
2. 相同版本的 `off` vs `profiling`，估计采集和上报带来的扰动；同时看 latency、吞吐、
   CPU/memory、请求/命令数量，不把 profiler 的代价藏在后台。
3. 优化前后两个版本都处于 `off` 的完整用户操作 benchmark，作为实际收益的主要证据；
   相同 profiling 设置下的阶段对比解释收益来自哪里。

检查关闭时没有 profiler 初始化、时间采集、exporter/线程/timer、DB 写入或新增 RPC；
检查开启后 expiry、上限、主动关闭、队列满、服务重启和旧客户端都按约定结束采集。
不把测试环境噪声下“未观察到差异”宣称为绝对零开销；报告样本量和不确定性。

## 1. 优先覆盖哪些操作

P0 是首批用户等待闭环；P1 是下一批协作、交互与结果交付；P2 是容量、依赖和运维诊断。
这是实施优先级建议，不代表当前已建立 SLO 或发现对应性能问题。

| 优先级 | 操作 | 用户侧起点 → 完成条件 | 需要单独展开的阶段 |
| --- | --- | --- | --- |
| P0 | 新建沙盒、唤醒已有沙盒 | 发起需要 workspace 的操作 → 本次所需能力可用 | 调度、reservation、group/锁等待、create/start、网络、workspace、setup；用 trigger 区分 task/files/terminal/fork |
| P0 | 打开/新建终端 | 点击入口 → 终端流连接且输入可用；首输出另记 | list/create/reattach、workspace wake、PTY spawn、前端 xterm 加载、流连接、输出解码与渲染 |
| P0 | 打开 Files 面板/展开目录 | 点击 → 对应目录条目或确认的空目录可见 | 缓存、授权、broker、workspace wake、network check、exec、目录遍历/stat、JSON、渲染 |
| P0 | 打开文件 | 点击文件 → 对应文件内容可见 | 上述共享开销、读取、传输、文本处理/高亮/虚拟化；记录大小区间与是否截断 |
| P0 | 提交第一轮/后续轮 | 点击发送 → 请求被接受；首活动和首文本分别记 | submission、queue、sandbox、resident new/resume/reuse、admission、prompt dispatch、事件送达/渲染 |
| P0 | 打开或切换已有会话 | 点击会话 → 最新消息可见且 composer 状态确定 | metadata、历史事件加载、分页/解析、渲染、live stream 接入；历史首屏和追到当前进度分开 |
| P0 | 停止任务 | 点击停止 → 停止请求被接受；实际执行停止另记 | CP cancel、heartbeat/通知送达、CLI interrupt、退出/turn boundary、清理/settle、UI 状态更新 |
| P1 | 保存文件 | 点击保存 → 该次写入被服务端确认，UI 显示一致结果 | 写 admission、workspace wake、exec/write、响应、后续 refresh；保存确认不命名为 fsync durability |
| P1 | 打开 Changes / Git diff | 打开面板 → 当前 diff 可查看 | broker、git status/diff/log、序列化、传输、diff 解析和渲染；区分变更文件数量/总字节 |
| P1 | workspace 变化可见 | 已知变更完成/refresh 触发 → Files/Changes 展示相应版本 | refresh 调度、debounce、请求、缓存失效、渲染；没有变更时钟/版本就只报告 refresh latency |
| P1 | 终端持续交互 | input 提交 → PTY write；服务端输出 → 浏览器处理 | input batching、HTTP、队列/背压、PTY、output stream、xterm 处理；受控实验单测按键到回显 |
| P1 | 断线重连 | 确认断线 → 重连成功并追到指定 cursor | reconnect backoff、auth、broker、buffer replay、实时恢复；terminal 和 agent stream 分开 |
| P1 | Fork | 点击 Fork → child 会话可用 | 幂等 reservation、group/member wait、native session fork、child CLI init、CP finalize、UI 导航；child 首轮另测 |
| P1 | 结果发布 | workspace 达到可发布边界 → PR URL 可见 | snapshot queue、writer freeze、capture、thaw、publish queue、凭据、Git push、GitHub/GitLab API、UI 刷新 |
| P2 | 登录与首屏 | 开始登录/打开应用 → 登录状态和任务列表可用 | OAuth 自身处理与用户停留分开、session exchange、config/models/projects 请求、浏览器加载/渲染 |
| P2 | 项目/仓库/分支选择及任务搜索 | 选择或查询 → 对应选项/列表可用 | debounce、缓存、DB、source-control API、分页；不能记录原始查询词或 repo 内容 |
| P2 | MCP 连接与调用 | 平台接到对应操作 → connection/result 可用 | resolve、connect/initialize、tool discovery、upstream request、timeout/retry；返回流单列 |
| P2 | host 接活与回收 | 注册/部署 → 可 claim；idle 条件满足 → 容器 stopped/容量释放 | image 获取、preflight、enrollment、readiness、heartbeat freshness、drain、stop、cleanup |
| P2 | Admin 数据与用量刷新 | 打开/请求刷新 → 数据可见 | CP/DB/Gateway 读取、后台刷新排队与执行、UI；同时标注采集时间和数据年龄 |

只有产品真实存在的操作才建主指标；例如未来若上线 Web app preview，再为启动服务、
路由可达和首屏分别定义 measurement。文件预览和应用预览不能共用一个含义模糊的指标。

## 2. 当前代码中值得注意的共享开销

### 终端

终端点击路径除了 server create，还包含终端列表、动态加载 xterm、PTY 流连接和
浏览器绘制。[TerminalWorkspace](../../web/src/components/features/agents/TerminalWorkspace.tsx)
与 [TerminalPane](../../web/src/components/features/agents/TerminalPane.tsx) 分别管理这些部分。
[terminalRuntime](../../web/src/components/features/agents/terminalRuntime.ts) 已做共享预加载，
因此必须区分前端模块 cache hit/miss，不能只测“热浏览器”。

终端 API 返回 terminal ID、stream HTTP 200、首个输出字节、shell 可接收命令是不同
milestone。首字节可能是 banner，shell 也可能不输出 prompt；不通过匹配 `$`/`#` 判断
ready。生产先记录 PTY-created、stream-connected、input-enabled、first-output 等可
证实的边界。shell-ready 如无可靠 integration 则为空，受控 benchmark 用固定 shell
和无副作用 marker 验证，不向用户终端插入测试命令。

input HTTP 确认只能证明写入链路完成，不能证明字符已回显或命令执行完。一般终端流
没有天然的按键/输出一一对应关系；生产统计写入延迟、输出处理延迟、积压和丢失，
按键到回显只在固定测试终端中测。

### 文件

[文件 API](../../cloud_agent/api/routes.py) 会做 ownership、repo entitlement 与 broker
解析。[broker](../../cloud_agent_host/workspace_broker.py) 为文件读取打开 workspace
operation、验证网络，再通过 [retained_workspace](../../cloud_agent_host/retained_workspace.py)
在 guest 内执行固定 Python helper。文件读取的 wall time 包含容器 exec 和解释器启动；
不能把 API 总耗时直接命名为磁盘 read latency。

[useWorkspaceFiles](../../web/src/components/features/agents/useAgentJobs.ts) 缓存已展开目录，
刷新会重读根目录、已展开目录及当前文件。展开目录的 cache hit、手动刷新和点击文件
是不同操作。快速切换丢弃旧请求结果是 superseded，不能伪报服务失败或零耗时成功。

本地已有 `scripts/bench_workspace_file_open.py`，可以承接文件路径的专项 benchmark。
它的 helper/stack/docker/live 模式分别有不同覆盖范围；live endpoint 计时仍不包含
真实浏览器交互和渲染。后续使用前分别验证，不将模拟栈结果当作线上数据。

### 沙盒被其他操作唤醒

[supervisor](../../cloud_agent_host/shared_session_supervisor.py) 的 `operation_opened()`
和 `terminal_opened()` 都会调用 `_ensure_running()`。因此点文件和点终端也可能触发
容器唤醒，无需有新的 agent attempt。现有 attempt-only measurement 无法覆盖全部操作。

例如一次打开文件用时 3 秒，可能是唤醒占了 2 秒，真正读文件很短；这些数字只是说明
归因方式的示例。文件的用户总等待应保留 3 秒，子阶段说明其中的唤醒成本，不把那 2 秒
从用户体验指标中扣除。多个操作共享同一次 wake 时，创建一个 wake operation，各自
记录等待并 link 到它；资源消耗和执行次数不复制给每个等待者。

## 3. 用一套 operation 模型连接这些测量

需要独立于 agent attempt 的 operation 身份：

```text
interaction_id: 一次用户意图，可含多次请求/重试
operation_id: 一次平台操作，如 files.open、terminal.create、sandbox.wake
parent_operation_id / links: 子步骤或共同等待的共享操作
attempt_id?: 只有关联 agent 执行时才有
thread_id? / sandbox_id? / terminal_id?: 按实际作用域关联
origin: browser | API | background
trigger: user | task | files | terminal | fork | idle | deployment
profiling_run_id, producer, clock_domain_id, schema_version
milestones, spans, outcome, missing_reason, retries, sampling_probability
```

浏览器生成的 correlation 只用于追踪，服务端身份、权限和资源归属仍从认证/存储校验。
operation 记录不能赋予读写或执行权限；终端和平台文件操作的资源也不能自动计入某个
agent member 的 turn。现有 eBPF/resource measurement 用 link 关联，保持原有 scope。

每个操作的 span 采用同一组公共名称：frontend-load、auth、DB/pool-wait、broker-RPC、
group/lock-wait、sandbox-wake、exec、operation-work、serialization、render。按实际
执行选择子阶段，不要求每个操作都跑一遍。已有 wake、新建沙盒和本轮 CLI 启动保持
不同动作语义。

每个版本化 operation contract 都要写清：负责代码、起点、成功终点、失败/取消终点、
时钟、包含的等待、分类维度、数据上限和验收场景。新增主要交互时把这个 contract
作为设计验收项，防止只留下“以后加 monitoring”。

## 4. 统一报告四类结果

1. **Latency**：成功达到对应 milestone 的 P50/P95、样本量与慢样本；有足够样本
   再解释 P99。首次/复用、running/stopped、数据大小、并发程度分开。
2. **Outcome**：成功、失败、timeout、用户取消、被新操作替代、页面离开/结果未知。
   后端成功但页面已离开的情况同时保留两个视角；不只统计成功返回的 HTTP 请求。
3. **Freshness / continuity**：状态多久没更新、断线多久、恢复到哪个 cursor、数据是否
   过期/截断。输出长时间静默可能是模型或命令正常运行，不能仅凭字节间隔判定故障。
4. **Pressure / overhead**：DB pool wait、broker queue/锁等待、线程池排队、host slots、
   CPU/memory/I/O 压力、stream buffer/丢弃量、每次操作的 Docker exec/inspect 数量。
   复用已有 host 快照；它是环境上下文，不是本次请求独占的资源账单。

开启的实验同时显示 measurement 覆盖率。已选中的排队任务、超时请求、丢失的浏览器
样本不能悄悄消失；latency 分布按完整样本给出，失败和未知另列数量/比例。范围外和
关闭时的操作不是 missing measurement，不纳入该实验的缺失率。

数据采集遵守：

- 浏览器测用户动作到本地可见状态；服务内部用各自的单调时钟。UI commit、输出处理
  callback、实际 paint 等来源不能混称完全相同的显示时刻，benchmark 验证代理边界。
- 跨机器只关联，不未经校准相减时间戳。父 span 与并行子 span 不重复求和；不能把
  API 总耗时再加一次它内部的 wake 耗时。不能把阶段 P95 相加得到总 P95。
- browser intent 是总等待的起点；只在真正发 HTTP 时开始会漏掉 debounce、前端模块
  加载、前置条件等待和重试 backoff。
- 默认不采集路径全文、文件内容、终端输入/输出、prompt、token 或原始命令。指标 label
  使用操作名、结果、大小桶等有界维度，实体 ID 仅用于受权明细关联。
- 开启时采集异步、有界、不会阻塞执行。浏览器观测属于不可信诊断数据，不能作为计费
  或授权依据。选择率和掉样可见；实验的成功率只描述本次实验 cohort。
- 减少 backend 工作若导致页面显示旧数据，必须在 freshness/正确性检查中显现。
  不能只凭 HTTP P95 下降就宣布用户操作变快。

## 5. 怎样落地并用于优化

先落实默认关闭的快路径、有范围/有效期的 run 与开关验收，再为用户明确提出的三类
操作加按需埋点，并复用公共阶段：

1. **sandbox prepare/wake**：沿用启动设计，补上 files/terminal 等非 attempt trigger。
2. **terminal open/create/reattach**：记录 server PTY 与 browser stream/input/first-output 边界。
3. **files list/open**：记录 browser 可见、CP/broker、wake、exec/helper 和数据量。

随后补齐 P0 的提交/会话打开/停止闭环，再按表推进 P1/P2。第一版可用结构化日志、
有界存储与导出验证，不以完成一个通用 tracing 平台或全量仪表盘作为前置任务。

每项同时有显式开启的真实操作 profiling 与固定 benchmark：小/大文件、少/多目录条目、冷/热前端模块、
running/stopped container、新建/重连终端、短/长会话、单并发/满容量。使用独立测试
workspace，不清生产缓存、不更改用户 shell，不向生产任务注入测试输入。

每次优化对照相同场景的 baseline/candidate，验证用户总等待和目标子阶段同步变化，
保留失败/超时/缺失率及原始样本。实验回归门槛在第一轮基线后确定；长期 SLO 暂不纳入。
已有 isolated
helper benchmark 负责解释该层成本，完整产品路径需要浏览器与真实 broker/Kata 测试。

实施 PR 仍按仓库要求完成 `make check`、涉及数据库时的 `make test-db` 及相应真实
集成测试。本清单只记录设计范围，没有宣称埋点、性能基线或线上验证已经完成。
