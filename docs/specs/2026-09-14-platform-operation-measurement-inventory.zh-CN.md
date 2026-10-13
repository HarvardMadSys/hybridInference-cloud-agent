# 平台操作测量清单

创建：2026-09-14；范围与接入契约更新：2026-09-18。
状态：**#258 已实现三类服务端采集、持久化、Admin API/UI 与有界投递；
浏览器终点仍待实现，分支尚未合并或部署。**
基于本地代码检查，扩展 [沙盒启动测量设计](2026-09-14-sandbox-startup-measurement-design.zh-CN.md)。

**2026-09-18 整合修订**：#218 的存储与 Admin 读面已经接到 #258 的统一采集器，
具体身份、预算、持久化、版本与交付契约见
[存储/Admin 整合设计](2026-09-16-operation-measurement-storage-admin-integration-design.zh-CN.md)。
`e77e3e6` 的历史
[五项 review 问题](2026-09-16-operation-measurement-e77e3e6-review.zh-CN.md)已经关闭；
身份已拆为 interaction_id 与每请求唯一 measurement_id。

目标：在按需开启的 profiling 实验中，每个主要用户操作都能回答：
**多久可用、是否成功、卡在哪一步、结果是否新鲜**。
执行中的 CPU、内存和 I/O 继续由已有 member-window/eBPF 体系解释；平台操作的等待由
前端和服务端阶段计时解释。每种测量都保留来源、范围和覆盖率。

**2026-09-14 范围修订：新增平台 profiling 默认关闭，优先服务前期性能定位、基线和优化
对比，不作为长期常驻采集。** 下文所有计时、operation ID、spans、报表和上报，仅针对
显式开启且选中的实验操作。此决定同时取代启动设计中原先的默认全量采集建议。

## 交付状态与首批范围

用户要求的首批范围是**沙盒启动、文件浏览/打开、终端打开**。三者共享测量方法，
区别在入口、成功终点和内部阶段。启动测量通过验收，不等于三类需求都已交付。

截至 2026-09-18，[#258](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/258)
仍为 OPEN、目标 dev，尚未合并或部署。下表以合并最新 dev 后的分支实现为准；
§3.1–§3.5 中的浏览器部分仍是待实现契约。

| 能力 | 已有内容 | 尚缺内容 |
| --- | --- | --- |
| attempt.startup | runner claim 请求前至 prompt 交给 supervisor；四路径、内部阶段、可选采集、自动入库与完整性协议 | CP 排队、浏览器等待、模型首输出等完整用户边界；真实四路径线上基线 |
| files.list / files.open | 独立 helper/stack/docker/live benchmark；环境范围明确的 baseline 文档；**CP 选择 + sandbox manager 阶段 + 响应片段透传** | 浏览器起点与内容可见终点、纯缓存命中的覆盖、真实整段开销实测 |
| terminal.open | 终端现有业务流程；本清单中的边界设计；**list/create/stream 的共同操作身份（`measurement_id`）、PTY 阶段、流式片段的响应头回传** | 前端 xterm 可交互终点、buffer replay 与新输出的区分、首输出观察窗口 |
| 共享采集器 | 默认关闭、run/期限/单进程预算、call ID、缺失与截断、有界异步导出；schema v3 通用记录与三类根操作 | 前端适配器、分布式名额分配 |
| 分析与展示 | 结构化日志、可选 JSONL、Postgres、Admin API/UI、启动采集器微基准 | 三类线上基线与浏览器端到端报告；sandbox manager 完整采集开销实测 |

服务端接入的落地位置、关闭路径不变性与明确未交付范围见
[启动设计 §9.3](2026-09-14-sandbox-startup-measurement-design.zh-CN.md)。
**服务端片段不等于用户端到端测量**：浏览器起点与 UI 终点未接入前，只能声明 server
覆盖，不能用它回答“用户等了多久”。

文件 benchmark 的 live 模式只计到公共 HTTP 请求返回，仍不包括浏览器渲染。
终端尚无同等已交付的专项 benchmark；不能以设计表或已有业务测试代替测量结果。
文件与终端接入见 §3，首批整体完成条件见 §5；其他 P0/P1/P2 项是后续扩展清单。

## 0. 可选 profiling 开关

### 两个状态，默认关闭

| 状态 | 行为 |
| --- | --- |
| `off`（默认） | 走原有业务路径，不初始化新增 profiler、分配 measurement ID、取计时值、构造 span/attributes、序列化或上报 profiling 数据 |
| `profiling` | 仅对本次实验选中的目标和操作计时；到截止时间或样本上限后结束 |

部署级主开关为 `AGENT_OPERATION_PROFILING_ENABLED`，缺省为 `false`。
**2026-09-14 已实现**（`apps/backend/cloud_agent_shared/operation_profiling.py`，目前接入 runner 的
`attempt.startup`；变量说明见 [ENVIRONMENT.md](../../ENVIRONMENT.md)）。仅设为 `true`
不代表所有请求自动被采集；还需配置有明确范围和截止时间的 profiling run
（`AGENT_OPERATION_PROFILING_RUN`，未配置或不合法时开关不生效并只记一条 warning）。
第一版通过测试/运维配置提供 run descriptor，
不要求先开发设置页面或完整的动态控制平台。主开关变更若需要重启，必须在实验准备
阶段完成，不能把重启后的冷缓存当成开关本身的开销。

实现的边界：supervisor 会上报本次 activation 对容器与 CLI 的实际动作（不依赖开关），
跨进程的内部时长以 duration 形式挂在所属阶段下，记录用 `producers` 标明实际贡献者。
选择发生在 runner 根操作处，所以 `max_operations` 目前是**单进程**预算，
不是清单要求的跨 replica 统一分配；名额在确认选中 attempt 后才消耗，未选中的
（空 claim、目标不匹配、claim 失败）会归还。产出写入结构化日志，并在配置
`AGENT_OPERATION_PROFILING_SINK` 时经**有界队列与后台线程**追加 JSONL；同一冻结
记录另经有界队列进入 Postgres 与 Admin，JSONL 不是入库前提。目标过滤要求每个配置
字段都匹配，未知字段直接拒绝该 run。
sandbox manager 侧需要同样打开开关并校验继承的选择才会贡献内部阶段。记录按每次调用的
call ID 判断完整性，并区分缺失、已应答但截断、正常复用而无需执行子阶段；完整协议
见启动设计 §9.1。日志和 JSONL 都经同一有界后台队列发布。
截止时间由 run descriptor 的绝对 `expires_at` 在首次读取时转成单调期限，服务重启
不会续期；到期后停止采集，记录在截止点收尾并标 partial（`measurement_complete=false`、
`profiling_stopped=expired`），已采部分保留。

一个 profiling run 的设计示例（不是可直接执行的现有命令）：

```yaml
mode: profiling
run_id: workspace-interaction-baseline-01
target: {sandbox_id: "<专用测试沙盒>"}
operations: [files.list, files.open, terminal.open]
expires_at: "<开启时生成的固定 UTC 截止时间，例如 15 分钟后>"
max_operations: 200
sample_rate: 1.0
```

这是当前文件/终端接入使用的形态；`sample_rate` 尚未实现，选择使用显式操作、目标、
期限与 `max_operations`。终端 list/create/stream_attach 是 terminal.open 的请求分类。

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
  浏览器传来的开关/header 不能直接让 CP 或 sandbox manager 启动采集，服务端独立校验 run 和范围。
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
| P0 | 打开 Files 面板/展开目录 | 点击 → 对应目录条目或确认的空目录可见 | 缓存、授权、sandbox manager、workspace wake、network check、exec、目录遍历/stat、JSON、渲染 |
| P0 | 打开文件 | 点击文件 → 对应文件内容可见 | 上述共享开销、读取、传输、文本处理/高亮/虚拟化；记录大小区间与是否截断 |
| P0 | 提交第一轮/后续轮 | 点击发送 → 请求被接受；首活动和首文本分别记 | submission、queue、sandbox、resident new/resume/reuse、admission、prompt dispatch、事件送达/渲染 |
| P0 | 打开或切换已有会话 | 点击会话 → 最新消息可见且 composer 状态确定 | metadata、历史事件加载、分页/解析、渲染、live stream 接入；历史首屏和追到当前进度分开 |
| P0 | 停止任务 | 点击停止 → 停止请求被接受；实际执行停止另记 | CP cancel、heartbeat/通知送达、CLI interrupt、退出/turn boundary、清理/settle、UI 状态更新 |
| P1 | 保存文件 | 点击保存 → 该次写入被服务端确认，UI 显示一致结果 | 写 admission、workspace wake、exec/write、响应、后续 refresh；保存确认不命名为 fsync durability |
| P1 | 打开 Changes / Git diff | 打开面板 → 当前 diff 可查看 | sandbox manager、git status/diff/log、序列化、传输、diff 解析和渲染；区分变更文件数量/总字节 |
| P1 | workspace 变化可见 | 已知变更完成/refresh 触发 → Files/Changes 展示相应版本 | refresh 调度、debounce、请求、缓存失效、渲染；没有变更时钟/版本就只报告 refresh latency |
| P1 | 终端持续交互 | input 提交 → PTY write；服务端输出 → 浏览器处理 | input batching、HTTP、队列/背压、PTY、output stream、xterm 处理；受控实验单测按键到回显 |
| P1 | 断线重连 | 确认断线 → 重连成功并追到指定 cursor | reconnect backoff、auth、sandbox manager、buffer replay、实时恢复；terminal 和 agent stream 分开 |
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

[文件 API](../../cloud_agent/api/routes.py) 会做 ownership、repo entitlement 与 sandbox manager
解析。[sandbox manager](../../apps/backend/cloud_agent_host/sandbox_manager.py) 为文件读取打开 workspace
operation、验证网络，再通过 [retained_workspace](../../apps/backend/cloud_agent_host/retained_workspace.py)
在 guest 内执行固定 Python helper。文件读取的 wall time 包含容器 exec 和解释器启动；
不能把 API 总耗时直接命名为磁盘 read latency。

[useWorkspaceFiles](../../web/src/components/features/agents/useAgentJobs.ts) 缓存已展开目录，
刷新会重读根目录、已展开目录及当前文件。展开目录的 cache hit、手动刷新和点击文件
是不同操作。快速切换丢弃旧请求结果是 superseded，不能伪报服务失败或零耗时成功。

本地已有 `scripts/bench_workspace_file_open.py`，可以承接文件路径的专项 benchmark。
它的 helper/stack/docker/live 模式分别有不同覆盖范围；live endpoint 计时仍不包含
真实浏览器交互和渲染。后续使用前分别验证，不将模拟栈结果当作线上数据。

### 沙盒被其他操作唤醒

[supervisor](../../apps/backend/cloud_agent_host/shared_session_supervisor.py) 的 `operation_opened()`
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
operation: 操作类型，如 files.open、terminal.open、sandbox.wake
operation_id: 一次平台操作的唯一 ID；接入时复用现有 measurement_id，不另造重复身份
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

每个操作的 span 采用同一组公共名称：frontend-load、auth、DB/pool-wait、sandbox-manager-RPC、
group/lock-wait、sandbox-wake、exec、operation-work、serialization、render。按实际
执行选择子阶段，不要求每个操作都跑一遍。已有 wake、新建沙盒和本轮 CLI 启动保持
不同动作语义。

每个版本化 operation contract 都要写清：负责代码、起点、成功终点、失败/取消终点、
时钟、包含的等待、分类维度、数据上限和验收场景。新增主要交互时把这个 contract
作为设计验收项，防止只留下“以后加 monitoring”。

### 3.1 三类操作共用机制，分别定义完成条件

下表保留已实现的 attempt.startup 边界，文件与终端行是待接入契约；不改变 #258
已有记录的语义：

| 操作名 | 根操作与起点 | 成功终点 | 必须区分的路径 |
| --- | --- | --- | --- |
| attempt.startup | 当前 runner 在发 claim 前创建 measurement | prompt 已交给 supervisor；不等同于模型接受或首输出 | new_group / resume_stopped / resume_running / reuse_live |
| files.list | 浏览器点击 Files 面板或展开目录；CP API 请求可独立发起服务端操作 | 对应请求的目录条目或确认的空目录提交到可见 UI；服务端另记响应完成 | root/expand/refresh、cache hit/miss、running/stopped |
| files.open | 浏览器选中文件；纯 API 请求从 CP 入口开始 | 当前仍选中的文件内容提交到可见 UI；服务端另记响应完成 | 文件大小桶、预览截断、running/stopped、请求替换 |
| terminal.open | 浏览器打开终端面板、创建/选择一个终端；纯 API 请求独立记录 | xterm 已挂载、对应流已连接、平台输入门已打开三者均成立 | create / reattach、模块冷/热、buffer replay、running/stopped |

浏览器终点记录具体来源：React commit 或 xterm 回调是可见性的代理边界，不伪称物理
屏幕绘制完成。自动 refresh 标为 background trigger；用户手动 refresh 才计用户等待。
终端可交互不等于 shell 已完成 rc 文件或命令能够立即执行，shell-ready 仍按 §2 单独取证。

实现时先把 operation_profiling.py 的选择、时钟、片段关联与 exporter 变成可由不同
producer 调用的公共能力，保持单一实现。producer 由发起者传入，允许 runner、CP、sandbox manager；
浏览器用相同契约的轻量适配器。公共记录使用 operation、outcome、failure_stage，
startup_path 等只属于启动 facts；不得把文件返回成功写成 startup_outcome=dispatched。
schema 升版，保留 #258 的 v1 读取兼容，不在同一 schema_version 下改变旧字段含义。
无需为了计时先迁移目录或引入新的 tracing 服务。

**已实现**：schema v3（`outcome`、`producer`、`producers`、`timing_sources`、
边界、coverage 与完整性）；
`startup_outcome` 仅作为 `attempt.startup` 的 v1 别名保留；CP 与 sandbox manager 作为 producer
接入，sandbox manager 片段经 `operation_call.py` 校验与应答。**未实现**：浏览器 producer 与其
轻量适配器（前端仍不上报，因此 browser 片段按缺失计）。

### 3.2 文件与终端的选择、关联和导出

文件/终端入口走 CP 与 sandbox manager，可能完全没有当前 agent attempt。
job_id 用于验证工作区归属，不表示把此次操作归入该 job 的 agent 启动或资源窗口。

1. 开启实验后，受认证的页面/session 配置下发最小实验描述，前端才加载 profiler。
   点击时记录本地起点和 interaction ID；关闭时不增加配置请求、监听器或上传。
2. CP 在已有业务请求中执行服务端选择。依次完成原有认证、ownership、entitlement
   和 sandbox_manager/host 解析，再确认目标匹配；不得为 profiling 增加权限豁免或探测命令。
   先做开关/操作等廉价预过滤；目标信息尚不齐全时可暂存候选起点，只有身份与范围
   确认后才消费名额、形成样本。后置绑定不匹配即丢弃候选，不输出范围外记录。
3. CP 为选中操作返回 measurement ID 与有期限的选择引用。一个 terminal.open 可包含
   list、create、stream 多个请求；后续请求继承同一选择，每次 RPC 生成独立 call ID。
   浏览器 ID 只用于关联，不能自行开启测量或声明 host、用户和 sandbox 归属。
4. sandbox manager 同时校验本地开关、run、操作和继承期限，deadline 取本地与继承期限较早者。
   files/terminals 路由都要支持可选上下文；无效元数据仅停用采集，业务权限校验保持原样。
5. 响应复用 #258 的 call ID 回声、profiling_recorded、profiling_truncated、stages。
   重试逐次登记；正常跳过是已应答且未截断；过期前收集的阶段继续保留。
   GET 文件和 SSE 流需设计可选 metadata/header 或旁路片段，不能更改文件内容、终端字节
   或把 PTY 原始输出作为测量数据。公共响应模型与客户端必须显式透传/消费 metadata，
   避免 CP 的 response_model 把 sandbox manager 片段过滤掉。
6. 浏览器和 CP 分别输出本地时长，用 measurement/interaction ID 关联；不把浏览器
   performance.now 与服务器单调时钟相减。缺浏览器样本时，服务端耗时仍可用，用户
   端到端覆盖标为不完整；纯 API 客户端只要求它声明的 server 范围。
7. 前端采集仅在开启且获准的实验中使用有界异步批次，服务端沿用有界 exporter。
   所有片段有数目/字节/期限上限，服务重启和长连接不得续期。诊断上传失败只影响覆盖，
   不阻塞用户打开文件或输入终端；上报入口的身份、run 和数据上限必须独立验证。

**名额与纯缓存命中必须显式设计。** #258 的名额只在 runner 进程内生效；文件/终端
扩展后由 CP 的根操作入口负责服务端名额，sandbox manager 子调用不再消费。单进程选择缓存须
有 TTL/数量上限，重试不重新抽样；在未实现共享 admission 前，实验 manifest 记录每个
admission 实例的预算，不能宣称 fleet-wide 上限。首轮可在专用单 CP 实例上验证。
**已实现的部分**：CP 根操作选择、服务端名额、有界 TTL 选择缓存、同名方法跳过与
重试各自登记片段。**未实现的部分**：跨实例共享 admission、浏览器预领名额，以及
不产生 HTTP 请求的纯目录缓存命中覆盖——该场景当前没有 server 片段，必须按缺失报告。
跨实例无法恢复原选择时，继续业务并标明片段不可用，不能用一次新的选择伪装连续测量。

目录纯缓存命中没有 CP 请求：前端需在开启实验时从可信配置/实验 admission 预领一小批
有范围、有绝对截止时间的浏览器样本名额，耗尽即停，不为 cache hit 发业务请求。
这些名额由 admission 从该 run 的预算预留并绑定会话；若后来发生 HTTP 请求，CP 校验并
复用同一名额，不能再扣一份。刷新页面不能获得无限新名额；
签发和校验方案是前端接入的一部分，未实现前只能声明 server 范围，不能承诺覆盖 cache hit。
本地命中只要求 browser producer，不应报告缺失 CP/sandbox_manager；发出网络请求后再要求相应片段。

### 3.3 文件浏览/打开的具体接入

实际调用顺序为 browser → CP → sandbox manager → supervisor.operation_opened() →
网络验证 → retained file helper → CP 响应 → browser UI。operation_opened 在 sandbox manager
内部，可能唤醒容器，不经过 runner.run_once()。

| 位置 | 要记录的边界 |
| --- | --- |
| useWorkspaceFiles / WorkspaceFiles | 点击、目录缓存命中、request generation、响应接收、当前请求对应内容的 UI commit |
| api/routes.py:get_agent_job_workspace_file | 选中请求的认证/归属与 entitlement、sandbox manager 解析、sandbox manager RPC、响应构造；框架发送完成若未观测则不命名为网络传输完成 |
| SandboxManagerClient.files / sandbox_manager.py:files | 上下文与片段透传、工作区 admission、operation_opened、group 等待与容器 wake、network check |
| retained_workspace.read_retained_file / ChatSandboxManager.exec_capture | exec 总耗时、helper 执行、读取或目录遍历、返回字节/条目数量桶 |

第一轮可把 docker exec + Python 启动 + read 作为可证实的组合阶段；如要再拆，需让
固定 helper 在选中时返回自己计的子时长，不增加第二次 exec 来探测，也不把组合阶段
误称为纯磁盘读取耗时。数据大小与条目数有界，文件路径与内容不进入 profiling 记录。

目录缓存命中仍记录用户可见时间。文件请求被较新的选择替代时记 superseded；响应晚到
且未被 UI 使用不能算成功可见。页面离开记 abandoned/unknown；错误、超时与空文件
分别处理。一个请求发现目标其实是目录时记录实际返回 kind 与 UI 结果，不伪称文件可见。
文件预览截断与计时截断使用不同字段，例如 content_truncated 与 measurement.truncated。

### 3.4 终端打开的具体接入

一次 terminal.open 从用户意图开始，包含终端列表和前置 workspace-ready 等待。
TerminalWorkspace 负责列出、新建、选择终端；TerminalPane 负责 xterm 和现有 SSE 流。
相关 HTTP 入口是 /jobs/{job_id}/terminals、/{terminal_id}/stream，以及对应 sandbox manager 路由。

| 位置 | 要记录的边界 |
| --- | --- |
| TerminalWorkspace / terminalRuntime | 点击、前置 ready 等待、list/create/reattach、模块加载冷/热、选中 terminal ID |
| CP terminal API / SandboxManagerClient | ownership/entitlement、sandbox manager 解析、每次 list/create/stream 请求与重试 |
| sandbox_manager.create_terminal / supervisor.terminal_opened | 工作区 admission、锁等待、容器 wake、network check、PTY/shell spawn、descriptor 返回 |
| TerminalPane / stream 客户端 | xterm mounted、stream connected、input enabled、首条输出接收、xterm 写入回调；buffer replay 与新输出分开 |

当前 stream 客户端需补可验证的连接建立回调；它来自对应请求的成功握手/流建立，
不等首条输出到达。安静的 shell 可以正常可交互却没有 first-output；后者单独标
未观察到，不能把终端打开计为超时。首输出 callback 不是 shell-ready 证据。
create 与 reattach 分开聚合；reattach 正常不生成 PTY-spawn 阶段，仍可得到完整片段。

测量在可交互、失败、取消或截止点结束；首输出可使用同一 run 截止前的短观察窗口，
不把整段终端会话纳入启动 span。断线重连另记子操作/后续操作，不把已完成的打开
时长改长。异步模块完成、旧 stream 的事件都必须匹配本次 interaction/terminal，
组件重挂载不能重复完结同一条记录。生产用户终端不注入 marker、按键或 shell 命令。

### 3.5 三类共同的验收矩阵

| 场景 | 通过条件 |
| --- | --- |
| profiling off | 无新增计时、测量对象、导出线程、前端 profiler 加载/监听/上传；业务行为一致 |
| 开启但未选中 | 入口预过滤直接跳过；后置绑定不匹配丢弃候选、归还名额，不导出范围外数据 |
| 配置错误、任一 producer 未开启、旧客户端 | 业务正常；已选中操作按范围报告缺失，不伪造完整 |
| 文件目录缓存命中、空目录、空文件、大文件 | 用户侧终点正确；缓存命中不要求 server 片段；内容截断不混同计时截断 |
| 文件快速切换、后台 refresh、页面离开 | 无旧请求误报可见；superseded/background/abandoned 分开 |
| 终端新建、热复用/reattach、安静 shell、模块冷/热 | 正常跳过仍完整；首字节不是可交互的前提；首输出与 replay 单列 |
| 文件/终端触发 stopped 容器 wake，并发共同等待 | 原操作包含全部等待，wake 因果关联；不重复累计共享执行成本 |
| 调用失败后重试、应答 call ID 不匹配 | 每次物理调用独立，后一次成功不掩盖前一次缺失 |
| 等锁时过期、sandbox manager 期限更短、阶段跨截止点 | 已应答但部分，保留先前阶段；空列表不能抹掉 skipped/truncated |
| exporter 队列满、线程启动失败、文件/日志阻塞、前端上传失败 | 有界丢样可见，业务不失败、不等待导出 |
| browser 未回报或只有纯 API 调用 | 分别判断 browser/server 所需覆盖，不能把 server latency 标为点击到可用 |

每类都要有真实业务路径的 on/off 对照；文件 helper 微基准与启动 recorder 微基准
不能代替 CP → sandbox manager → 前端整段采集开销。完整结果需覆盖选中、失败、超时、部分与
缺失的分母；不只展示最快的成功请求。

## 4. 统一报告四类结果

1. **Latency**：成功达到对应 milestone 的 P50/P95、样本量与慢样本；有足够样本
   再解释 P99。首次/复用、running/stopped、数据大小、并发程度分开。
2. **Outcome**：成功、失败、timeout、用户取消、被新操作替代、页面离开/结果未知。
   后端成功但页面已离开的情况同时保留两个视角；不只统计成功返回的 HTTP 请求。
3. **Freshness / continuity**：状态多久没更新、断线多久、恢复到哪个 cursor、数据是否
   过期/截断。输出长时间静默可能是模型或命令正常运行，不能仅凭字节间隔判定故障。
4. **Pressure / overhead**：DB pool wait、sandbox manager queue/锁等待、线程池排队、host slots、
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

首批三类操作的完成标准是同时具备：**可信的边界、按需采集、阶段归因、完整性信息，
以及能跑通真实操作的验证**。下列步骤组织实现依赖，不要求每一步单独拆 PR：

1. **启动切片（#258，已实现）**：保持 §0 的关闭快路径和启动片段回归；真实四路径基线
   与 sandbox manager 自身开销仍待补，不因微基准通过就宣称启动已变快。
2. **公共记录与 CP 接入（已实现）**：schema v3、CP 根操作选择与名额、选择缓存、
   交互关联、有界持久化与 Admin 读面。
3. **文件和终端服务端接入（已实现）**：§3.3–§3.4 的后端阶段已实现并标为
   **server-only**；成功、失败与 stream attach 均在测量结束处入库。前端起点、
   UI/终端终点仍待实现，实现前不得声明端到端覆盖。
4. **服务端整体验收（已实现）**：覆盖真实 Postgres/HTTP/Admin 链路、无 JSONL 的启动
   投递、持续 SSE、数据库拥塞、关闭快路径和 collector 开销。三类真实线上操作的
   P50/P95、覆盖率与失败率是启用该能力后的首轮 profiling 产物，不伪装成代码测试结果。

随后补齐 P0 的提交/会话打开/停止闭环，再按表推进 P1/P2。第一版可用结构化日志、
有界存储与导出验证，不以完成一个通用 tracing 平台或全量仪表盘作为前置任务。

每项同时有显式开启的真实操作 profiling 与固定 benchmark：小/大文件、少/多目录条目、冷/热前端模块、
running/stopped container、新建/重连终端、短/长会话、单并发/满容量。使用独立测试
workspace，不清生产缓存、不更改用户 shell，不向生产任务注入测试输入。

每次优化对照相同场景的 baseline/candidate，验证用户总等待和目标子阶段同步变化，
保留失败/超时/缺失率及原始样本。实验回归门槛在第一轮基线后确定；长期 SLO 暂不纳入。
已有 isolated
helper benchmark 负责解释该层成本，完整产品路径需要浏览器与真实 sandbox_manager/Kata 测试。

实施 PR 仍按仓库要求完成 `make check`、`make test-db` 及 web 类型/单测/构建检查。
当前实现不包含浏览器终点，也不宣称三类端到端基线、缓存命中覆盖或线上优化结果；
哪些能力已交付、哪些仍是缺口以本节和启动设计 §9.3 为准。
