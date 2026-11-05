# 结构重构：显式状态机、成员启动面、application 层和 host 协议 v2

日期：2026-11-05。状态：提议，待评审。

设计核对基线：远端 `dev`，提交 `3b40dc602`（已含 #1103：agent 自己开 PR，删除 publisher），并假定 #1104（一个 MCP 概念，head `14078a8d1`）随后合并。文中的行号都是基线上的；只在 #1104 里存在的东西会注明。

相关：[Agent 自己开 PR](2026-11-04-agent-native-pull-requests.zh-CN.md)、[一个 MCP 概念](2026-11-04-mcp-one-concept.md)（#1104）、[Computer use 基础能力](2026-11-02-computer-use-base-capability.md)、[原生会话、Fork 与共享工作区](2026-08-31-native-session-fork-shared-workspace-design.md)、[代码结构](../../developer/code-structure.md)、复杂度棘轮 #771。

## 1. 结论

这次重构不改产品行为，不放宽 AGENTS.md §7 的任何安全边界，也不给数据库表和线上字段改名。改的是代码怎么组织，以及 host 和控制面之间怎么交换状态和凭据。

依赖方向本身是干净的：api/ 和 application/ 不写 SQL，控制面和 host 互不 import，复杂度只降不升。问题集中在下面六条主线：

- **A. 把隐式状态机写出来。** sandbox group 的生命周期在 host 上是十几个 bool，在控制面上是散在 SQL 里的字符串字面量。第一步只把"现在能不能做"的判断集中到一处，行为不变。换掉底层表示要另写 spec，从代码里逐一推出转移表。
- **B. 成员启动面。** sandbox 里每多一种平台能力（浏览器、sandbox MCP、GitHub relay、MCP program 的 key），都要手工改同样五个地方，其中脱敏列表已经两次漏掉新加的凭据。改成每种能力一个"启动面"（facet），由它自己声明字段、指纹、准备步骤、环境变量和要脱敏的值。**这条排第一，** 因为接下来的功能开发还会不断碰到这五个地方。
- **C. 控制面 application 层。** application 不再用 `HTTPException` 报错，storage 不再用 `ValueError("code")` 传错误码；claim 和 Fork 的用例从路由里搬出来；租约和 residency 的 fence 各收成一份共用实现。
- **D. host 端的上帝对象。** supervisor、sandbox manager、`ChatSandboxManager` 和 runner 按职责拆开，组件之间只走公开接口。
- **E. host 协议 v2。** 加协议版本号；统一 fence；统一凭据封装；控制面通知 host 的六条路径收敛成一个持久化的命令队列。这一条改的是通信、调度和 fence 语义，本文只给方向：先补混合版本测试，每一项都要另写 spec 才能排期。
- **F. 模块整理和残留清理。** 大多是纯移动。另外，#1104 合并后，MCP 的 `managed-worker` 和 `user-connector` 两种 location 已经没有运行时，按删除 publisher 的方式把它们删掉。

交付见第 13 节，分成两部分：

- **现在承诺要做的**：B、C、D，以及 A 的第一步。它们都是局部改动，行为不变。
- **先写设计、评审后再排期的**：A 的第二步和整个 E。

F 在其他工作的间隙穿插进行。每个 PR 单独可回滚，并遵守"每一侧只兼容上一版"的滚动升级规则。

## 2. 为什么现在做

### 2.1 基线

| 指标 | 基线值 |
|---|---|
| 后端 Python（不含迁移和测试） | 约 22.4 万行 |
| 超过 1,500 行上限的后端模块 | 17 个 |
| 带 `noqa … #771` 豁免的后端函数 | 100 个 |
| `SharedChatSessionSupervisor` | 从 `shared_session_supervisor.py:435` 开始约 4,700 行，103 个方法 |
| `sandbox_manager.create_app` | 从 `sandbox_manager.py:1364` 开始约 1,900 行的闭包，约 60 个路由 |
| application/ 里的 `HTTPException` 引用 | 51 处，分布在 5 个模块 |
| storage 里内联比较 `g.state` / `g.deletion_state` 的 SQL | 52 处 |
| `ports/` 模块 | 43 个，除 `accounts` 外，每个都是对应 repository 公开方法的一比一照抄 |
| `cloud_agent/` 顶层平铺模块 | 82 个，约 2.5 万行 |
| `cloud_agent_host/` 平铺模块 | 116 个（含 `__init__`；11-02 的 `121d75240` 时是 106 个） |

### 2.2 一周四个能力，每次改同样五处

过去一周往 sandbox 里加了四种平台能力：

| PR | 能力 | 它新增的凭据或状态 |
|---|---|---|
| #1093 | sandbox 浏览器 | 每次启动成员前确保浏览器服务在运行 |
| #1097 | sandbox 里的 MCP 服务器 | `sandbox_mcp_servers`、slot admission |
| #1103 | GitHub relay | residency 的 `agh.` 占位凭据、git 作者身份、平台说明 |
| #1104 | MCP program 的 key | `SandboxMcpServer.secret_env`、`${NAME}` 引用 |

每一个都要分别改下面五处：

1. claim 或 residency 的 schema（`schemas/workers.py`、`schemas/hosts.py`）；
2. `ResidentLaunch`（`resident_transport.py:180`），现在有 25 个平铺字段；
3. `ResidentTransport.incompatible_fields`（`resident_transport.py:379`），一份手写的 16 项名单，决定什么变化需要起新进程；
4. `_prepare_runtime_helpers`（837 行）、`_member_env`（870 行）、`_launch_context`（917 行），在启动前分别启动服务、注入环境变量、追加平台说明；
5. `claim_secret_values`（`claimed_job.py:196`），runner 的输出脱敏列表，由三个 `.secrets()` 方法手工拼出来。

第 5 处已经两次漏掉新加的凭据：

- #1103 之前，sandbox 的 `github_token` 一直不在这份列表里，直到 #1103 把它整个删掉。
- #1104 在 `14078a8d1` 时，claim 会把 program 的 key 解密进 `SandboxMcpServer.secret_env`，但 `claim_secret_values` 没有收录它。`claimed_job.py:99` 的注释也还写着 "No secret: those stay remote"。

这不是谁粗心，而是结构让"漏掉"成为默认结果。下一批功能（computer use 第 3 阶段的 live view、program 的工具审批、更多 MCP 预设）还会继续往这五处加东西。

### 2.3 问题互相牵连

- **状态机没写出来，代码就只能大。** 状态散在 flag 里，每个操作都得自己判断"现在能不能做"。supervisor 里抛 `SessionBusy` 的地方有 54 处，三个准入谓词各自用不同方式组合这些 flag。这些判断没有一个统一的落点，类就拆不开。
- **错误用 HTTP 表达，用例就离不开路由。** `application/pending_messages.py:161` 在 goal 和恢复 worker 里 catch `HTTPException`，用 `status_code >= 500` 判断是不是暂时不可用，再从 `exc.detail["error"]["message"]` 里取原因，而这两处根本没有 HTTP 请求。用例想搬进 application，可写法还是 HTTP 的。

## 3. 原则

- **每个 PR 行为不变，除非标明是协议改动。** 测试只改 import 和 patch 路径，不改断言（AGENTS.md §5 第 3 条）。
- **纯移动和逻辑改动分开提 PR。** 移动的 PR 只改 import 和路径，审阅者能直接对比 diff。
- **不留 re-export shim。** 移动时在同一个 PR 里改掉所有调用方，包括测试里按字符串写的 `mock.patch` 目标。现在 `chat_sandbox.__all__` 转导出 27 个模型名，`runner.py` 也转导出一批名字，这些正是要清理的残留。
- **协议改动走 N/N-1。** 兼容代码只放进每个方向一个的 compat 模块，都标 `COMPAT(remove-after=…)`。
- **每做完一步，就把它保证的东西变成测试。** 例如不许出现内联状态字面量、application 不许 import fastapi、每种凭据都必须进入脱敏集合。这些测试和现有的大小棘轮一样只降不升。

## 4. 术语

AGENTS.md §6 写的是 "a job is the user's task"，这是错的：`agent_jobs` 带 `turn_no` 和 `parent_job_id`，基线迁移有 CHECK 约束 `(turn_no = 1) = (parent_job_id IS NULL)`（`0001_schema_baseline.py:193`），所以一个 job 就是一轮。同一个对话在代码里叫 thread，在 port 名里叫 conversation，在 docstring 和界面上叫 chat，在 AGENTS.md 里叫 task。

线上字段和表名不改，只统一文档和新代码里的叫法：

| 统一叫法 | 代码和线上的名字 | 含义 |
|---|---|---|
| 对话 | thread、`thread_id` | 用户的一次会话，包含多轮 |
| 轮 | job、`job_id`、`turn_no` | 一条用户消息和 agent 对它的处理 |
| attempt | `attempt_id`、`attempt_no`、`lease_generation` | 一轮在一台 host 上的一次执行；租约 fence 由它承载 |
| sandbox group | `agent_sandboxes`、`sandbox_id`、`sandbox_generation` | 一个保留容器和它的工作区 |
| 成员 | membership、member | group 里的一个对话，有自己的原生会话和 CLI 进程 |
| activation / residency / process | 各自的 `*_generation` | 成员的容器激活、常驻占位、CLI 进程三个层次 |
| 占位凭据 | `amr.`（MCP）、`agh.`（GitHub） | 指名一个 residency 的 bearer，只有对应的 relay 认，每次请求都重新解析 fence |

`generation` 这个词不许再单独出现，新字段一律写全，例如 `sandbox_generation`、`residency_generation`。现在 `RetainedSandboxBeat` 里的 `generation` 指 sandbox 的代数，`ResidencyBeat` 里的却指 residency 的代数，第 10.2 节统一 fence 后会消掉这种歧义。

交付：第 0 阶段修正 AGENTS.md §6，并把这张表放进 `docs/developer/code-structure.md`（同步更新中文译文）。

## 5. 主线 A：sandbox group 生命周期

### 5.1 host：`_Group` 的状态分三类

`shared_session_supervisor.py:332` 的 `_Group` 里和生命周期有关的字段，按含义分四类：

| 类别 | 字段 | 性质 |
|---|---|---|
| 进行中的生命周期操作 | `starting`、`stopping`、`snapshotting` | 正在做的一件事，做完就清除 |
| 事实记录 | `start_failed`、`container_lost` | 过去发生过什么；不随下一次操作开始而清除 |
| 未完成的义务（屏障） | `initial_archive_preservation_pending`、`stop_archive_pending`、`stop_archive_blocked`、`orphan_finalization_pending`、`orphan_marker_unreadable`、`start_archive_completion_pending` | 可以同时存在多个，各自等某件事做完才清除 |
| 占用者 | `operation_tokens`、`terminal_tokens`、成员的 `lease_token` 和 `current_turn`、`lifecycle_check` | 正在使用这个 group 的操作 |

**`start_failed` 是记录，不是阶段。** 带 `retry_failed` 的重试会在 `start_failed` 仍为真时再次进入启动（1127 行），直到这次启动结束才重写它（1265 行）。所以 `starting` 和 `start_failed` 会同时成立。11-04 那一版把它们都当成互斥的阶段，这是错的（见第 16 节）。

**`snapshotting` 名不副实。** #1103 删掉共享快照捕获以后，只剩 `prepare_environment`（3397 行）和 `capture_initial_archive`（3561 行）还会设置它。它表示的是"一个独占工作区的操作正在进行"，所以归入进行中的操作。

三个准入谓词 `_is_quiescent`（4833）、`_require_deletion_lifecycle_admission`（4848）、`_require_mutation_admission`（4864）各自把这四类混在一起判断。

**还没有证明的：** 进行中的操作是否同一时刻至多一个，`container_lost` 和哪些状态可以并存。这些不能靠推断，也不能靠"staging 上一周没报警"，要从全部 40 处赋值逐一推出来（见下面的 A2）。

目标结构放在新模块 `cloud_agent_host/group_lifecycle.py`。下面的形状只是方向，具体的转移表由 A2 的 spec 确定：

```python
class LifecycleOperation(Enum):
    STARTING = "starting"
    STOPPING = "stopping"
    ENVIRONMENT_PREPARATION = "environment_preparation"
    INITIAL_ARCHIVE = "initial_archive"

class Barrier(Enum):
    INITIAL_ARCHIVE_PRESERVATION = "initial_archive_preservation"
    STOP_ARCHIVE_PENDING = "stop_archive_pending"
    STOP_ARCHIVE_BLOCKED = "stop_archive_blocked"
    ORPHAN_FINALIZATION = "orphan_finalization"
    ORPHAN_MARKER_UNREADABLE = "orphan_marker_unreadable"
    START_ARCHIVE_COMPLETION = "start_archive_completion"

class Operation(Enum):          # 请求准入的操作
    MUTATION = ...      # 写工作区、开一轮、Fork
    DELETION = ...      # 管理员删除
    QUIESCENCE = ...    # 判断是否空闲、能不能停
    ...

@dataclass
class GroupLifecycle:
    operations: set[LifecycleOperation]     # 若 A2 证明至多一个，再收窄为 Optional
    last_start_failed: bool
    container_lost: bool
    barriers: set[Barrier]
    generation: int
    check: LifecycleCheck | None

    def admit(self, op: Operation, *, allow_lifecycle_check: bool = False) -> None:
        """按准入表决定；拒绝时抛 SessionBusy 或 RetainedChatLost，消息取自表。"""

    # 上面这些字段只能通过下面的方法修改
    def begin(self, op: LifecycleOperation) -> None: ...
    def end(self, op: LifecycleOperation, *, failed: bool = False) -> None: ...
    def raise_barrier(self, barrier: Barrier) -> None: ...
    def clear_barrier(self, barrier: Barrier) -> None: ...
```

- **准入表是一张静态表**，写明每个 `Operation` 会被哪些操作、记录、屏障和占用者拦住，以及拦住时给什么消息。三个谓词和散落的 `SessionBusy(...)` 都改成查这张表。测试直接对着表写，每行一个用例。
- **token 和租约仍然归各自的组件管。** `admit` 只额外读一个"有没有占用者"的布尔值，供 `QUIESCENCE` 判断。

分两步做：

1. **A1：外观层，行为不变。** `GroupLifecycle` 先作为现有 flag 的外观，属性读写的还是原来的字段；三个谓词搬进 `admit`。A1 不引入任何新的不变量，只把判断集中到一处。另外按组合记一条计数日志，作为 A2 的佐证，但不作为 A2 的依据。
2. **A2：替换，先写单独的 spec。** spec 要列出全部 40 处赋值、每个转移的前置条件，以及哪些组合合法、各自为什么合法。staging 的计数日志用来核对 spec 是否漏了组合。spec 评审通过后，才把 flag 换成新字段，并加测试：`group_lifecycle.py` 之外不许给这些字段赋值。

### 5.2 控制面：`agent_sandboxes` 的状态

控制面有两条互相独立的轴：

- `state`：`provisioning`、`starting`、`running`、`stopping`、`stopped`、`stopped_dirty`、`lost`、`cold`、`restoring`；
- `deletion_state`：`none`、`pending`、`archived`、`deleted`、`failed`。

还有一个 `snapshot_state`，#1103 之后永远是 `quiescent`，由下一版的迁移删掉。

现在唯一的常量是 `_sql/hosts.py:150` 的 `DELETED_GROUP_STATES`。反倒是顶层的 `cloud_agent/host_pool.py` 里有一张像样的转移表，可以照着做。

目标：

- **`domain/sandbox_group.py` 定义两个枚举和允许的转移。** 再从枚举生成 SQL 片段常量，写法照 `DELETED_GROUP_STATES_SQL`；storage 只引用这些常量。
- **两张表的写入集中到 `storage/_sql/sandbox_transitions.py`。** 现在 `agent_thread_sandbox_memberships` 由 11 个 storage 模块写，`agent_sandboxes` 由 7 个写，`agent_jobs` 由 9 个写。每个转移函数都按"锁行、按 domain 转移表校验、写入"的顺序执行。
- **一轮的状态同样处理。** 状态值是 `queued`、`waiting`、`running`、`succeeded`、`failed`、`cancelled`。`domain/execution.py` 现在只有 41 行常量，给它补上转移表。
- **棘轮测试：** 内联的状态字面量（基线 52 处）和写这几张表的模块数，都只许下降。

### 5.3 策略从 repository 里搬出来

repository 只负责加锁、读和写，决定交给纯函数。

例子是 `repositories/sandbox.py:1163` 的 `fail_admin_sandbox_deletion`。它在一个事务里同时做了五件事：

- 归一化重试类别；
- 按文件开头的常量算指数退避；
- 检查重试预算和时间窗；
- 拼出给管理员看的消息；
- 映射终止原因。

改成三步：repository 锁行并读出当前值；调用 `domain/sandbox_deletion.py` 的纯函数 `decide_deletion_failure(row, failure, now) -> DeletionDecision`；在同一个事务里写回结果。`compute_resources.py` 里调用 `domain.compute` 的那几处已经是这种写法。

repository 直接读运行时设置的地方，改成由调用方把值传进来。这些地方是：

- `goals.py:572,645`；
- `goal_wakeups.py:230`；
- `host_pool.py:1555`。

## 6. 主线 B：成员启动面

### 6.1 现状

一个成员 CLI 启动时要准备的东西，分散在三个层次：

| 层次 | 位置 | 内容 |
|---|---|---|
| 线上 | `WorkerClaimResponse`、`HostResidencyEstablishResponse` | 各种能力的字段平铺在顶层 |
| host 解析 | `ClaimedJob`（`claimed_job.py`）| 把线上字段解析成类型 |
| 启动 | `ResidentLaunch`（`resident_transport.py:180`）| 25 个平铺字段：推理、MCP relay、sandbox MCP、GitHub relay、git 身份、环境、插件 |

再往下是四段各自独立的手工代码：

- `incompatible_fields`，手写的兼容性名单；
- `_prepare_runtime_helpers`，启动浏览器服务、写 sandbox MCP admission；
- `_member_env`，注入环境变量和 GitHub relay 设置；
- `_launch_context`，追加平台说明。

脱敏列表另在 `claimed_job.py`。

所以加一种能力，就要同时记得改上面所有这些地方。漏掉任何一处都不会报错，只会出现以下几种结果之一：

- 改了配置但进程没重启（漏了 `incompatible_fields`）；
- 服务没启动（漏了 `_prepare_runtime_helpers`）；
- 值进了持久化输出（漏了脱敏列表）。

### 6.2 目标：每种能力一个 facet

```python
class LaunchFacet(Protocol):
    """一种平台能力在一次成员启动里的全部事实。"""

    name: ClassVar[str]          # 出现在不兼容原因里的名字，例如 "mcp"、"github"

    def fingerprint(self) -> Hashable:
        """它变了，就必须起一个新的 CLI 进程（原生会话照常续接）。"""

    def prepare(self, ctx: PrepareContext) -> None:
        """启动前由可信的 host 在容器里做的事：启动服务、写 admission。默认什么都不做。"""

    def env(self) -> Mapping[str, str]:
        """给成员 CLI 的环境变量。默认为空。"""

    def secrets(self) -> Mapping[str, str]:
        """这次启动带进 sandbox、持久化输出里必须遮掉的值。"""


@dataclass(frozen=True)
class ResidentLaunch:
    core: LaunchCore                      # thread、runtime、model、slot、residency_generation、native_session_id
    facets: tuple[LaunchFacet, ...]       # 每种能力至多一个

    def facet(self, kind: type[F]) -> F | None: ...
```

现有字段按能力拆成这些 facet，每个放在 `cloud_agent_host/launch/` 下的一个模块里：

| facet | 现有字段或步骤 |
|---|---|
| `InferenceFacet` | `credential`、`inference_base_url`、`inference_protocol`、`inference_source`、`inference_client_protocol`、`inference_local_token` |
| `McpRelayFacet` | `mcp_server_ids`、`mcp_base_url`、`mcp_credential` |
| `SandboxMcpFacet` | `sandbox_mcp_servers`；`prepare` 写 admission、启动 gateway 和 slot runner（现在的 `admit_sandbox_mcp`）；`secrets` 包含 #1104 的 `secret_env` |
| `GitHubRelayFacet` | `github_credential`、`github_repo`、`git_author_*`、`git_coauthor`；`env` 来自现在的 `github_relay_env` |
| `EnvironmentFacet` | `environment` |
| `PluginsFacet` | `plugins` |
| `BrowserFacet` | 没有字段；`prepare` 就是现在的 `ensure_browser_service` |
| `PlatformInstructionsFacet` | 现在的 `platform_instructions` |

这样，前面那几段手工代码都由 facet 推出来：

- **兼容性。** 两次启动按 `name` 比较每个 facet 的 `fingerprint`，不同的 `name` 就是不兼容的原因。手写名单删掉。迁移期间加一个测试，断言推出来的结果和旧名单完全一致。
- **准备步骤。** `_prepare_runtime_helpers` 变成按固定顺序调用每个 facet 的 `prepare`。顺序写成一张表，因为 #1097 的 spec 要求 gateway 先于 slot runner 启动。
- **脱敏。** runner 的脱敏集合等于 claim 里所有凭据（第 10.3 节）加上所有 facet 的 `secrets()`。`claim_secret_values` 删掉。
- **防漏测试。** 用反射检查每个 facet 的 dataclass：类型标为 `Secret` 的字段，都必须出现在 `secrets()` 的返回值里。以后再加凭据，忘了脱敏就过不了测试。

**facet 不认识 CLI。** facet 只提供数据和与 CLI 无关的步骤。一个能力怎么接进某个 CLI（Claude 的 `--mcp-config`、Codex 的 `-c mcp_servers`、pi/OpenCode/Kilo 的 `AGENT_MCP_SERVERS`、平台说明追加在哪个参数上），仍由 `runtimes/<cli>/` 的适配器决定：适配器用 `launch.facet(McpRelayFacet)` 取出它关心的 facet。现在 `with_platform_instructions(runtime_id, argv, env, …)` 这种按 `runtime_id` 分支的写法，挪进各个适配器里。

**以后加一种能力的清单**写进 code-structure.md：

1. 一个 facet 模块；
2. claim 或 residency 的一个字段（凭据走第 10.3 节的封装）；
3. 需要它的 CLI 适配器里加接线。

不需要改任何其他地方。

### 6.3 步骤

1. **B0：立即修复，不必等这次重构。** 在 #1104 合并前把 `secret_env` 加进 `claim_secret_values`，并改掉 `claimed_job.py:99` 的注释。这是一个小改动，可以直接放在 #1104 里。
2. **B1：引入 `Secret` 类型。** 引入 `Secret` 包装类型（`repr` 时遮掉内容），claim 里所有凭据字段都改用它；再加一个测试：`ClaimedJob` 里每个 `Secret` 字段都进入脱敏集合。行为不变。
3. **B2：引入 facet。** 引入 `LaunchFacet`，把现有字段搬进各个 facet，兼容性名单改由 facet 推出（用测试锁定和旧名单一致）。行为不变。
4. **B3：搬步骤。** 把 `prepare` 和 `env` 搬进 facet，按 `runtime_id` 分支的代码搬进适配器。`resident_transport.py` 里只留 transport 本身和 Fork；Fork 在 8.6 里再搬走。

## 7. 主线 C：控制面 application 层

### 7.1 错误

**application 和 domain 抛领域异常。** 新建 `cloud_agent/domain/errors.py`：

```python
class Refused(Exception):
    """调用方能看懂的拒绝：参数不对、没权限、状态冲突。"""
    kind: Literal["invalid", "forbidden", "not_found", "conflict"]
    type: str        # 线上的 error.type，例如 "model_connection_required"
    message: str

class Unavailable(Exception):
    """暂时做不了，可以重试。"""
    type: str
    message: str
    retry_after_seconds: int | None
```

- **映射到 HTTP 只在一处做。** `api/errors.py` 注册一个 exception handler：`kind` 映射成 400、403、404、409，`Unavailable` 映射成 503。个别 `type` 要用别的状态码时，写在同一个文件的覆盖表里。
- **响应体不变**，仍然是 `{"error": {"type", "message"}}`。所以 `openapi.json` 和断言响应体的测试都不用动。
- **后台流程按类型 catch。** `application/pending_messages.py` 不再看 `status_code`，改成 catch `Refused` 和 `Unavailable`。
- **按模块迁移。** 依次是 `job_admission`（25 处）、`session_fork`（10）、`follow_ups`（8）、`model_connections`（5）、`pending_messages`（3）。迁完后，`test_application_boundaries.py` 加一条规则：application/ 和 domain/ 不许 import `fastapi` 或 `starlette`。
- **api/ 自己的写法顺手统一。** 现在有 254 处 `HTTPException(`、141 处手写的 `"error": {` 字典，`typed_error` 只用了 33 次。碰到相应文件时顺手改，不单独开 PR。

**storage 的错误码改成枚举。** 现状：

- `compute_resources.py` 有 112 处 `raise ValueError("code")`；
- api 按 `str(exc)` 查 `api/_compute_route_policy.py:18` 的 `_STORAGE_ERRORS`；
- application 里有 7 处 `str(exc) == "operation_lease_lost"` 这样的比较。

一个无关 bug 抛出的 `ValueError`，只要文字恰好相同，就会被当成某个错误码处理。改成：

```python
# cloud_agent/ports/errors.py
class ConflictCode(StrEnum):
    OPERATION_LEASE_LOST = "operation_lease_lost"
    ...

class StorageConflict(ValueError):
    code: ConflictCode
```

迁移期间 `StorageConflict` 继承 `ValueError`，现有的 `except ValueError` 照常生效。全部迁完后改为继承 `Exception`，并加测试禁止出现 `str(exc) ==`。

### 7.2 用例从路由搬进 application

- **claim 交接。** `api/worker.py:674` 的 `_hand_over_claim` 有约 250 行，带三个复杂度豁免。它在路由模块里依次做这些事：
  - owner 准入和仓库授权检查；
  - 环境和 SSH 的 host 能力检查；
  - 拒绝时的状态转移（`release_claim`，或置为 FAILED）；
  - 签发凭据；
  - 解析 MCP 别名；
  - 拼 prompt。

  把它搬到 `application/turn_claim.py`，返回 `ClaimHandover | ClaimRefusal(action=release|fail, reason)`，路由只负责映射结果。等第 10.5 节做完，其中的能力检查还会挪到 claim 之前。
- **Fork。** 资格检查在 `api/turns.py:931` 的 `fork_agent_job` 里，准备和收尾在 repository 里，物化在 `application/session_fork` 里。资格规则统一搬进 `application/session_fork`。
- **MCP OAuth。** `api/mcp_connections.py:1012` 的 `authorize_connection` 包括 discovery、客户端注册和加密，搬到 `mcp/oauth_authorization.py`。

### 7.3 ports 精简

现状：

- 43 个 port 模块里，除 `accounts` 外每个都一比一照抄 repository 的公开方法，约一半的 docstring 是逐字复制的；
- 没有任何 port 有第二个实现，测试也不走 port：`tests/support/storage.py:99` 的 `AllCapabilities.__getattr__` 对任何名字都返回同一个 fake；
- 约 230 处返回类型是 `dict[str, Any]`，调用方仍在读原始数据库列；
- 调用方实际依赖的是自己声明的窄 Protocol，这样的 Protocol 在 `cloud_agent/` 里已有 126 个，例如 `api/worker.py:239` 的 `_WorkerStorage`。

决定：**不一次性删除这一层，但新代码从现在起不再照抄 port。** 以后每个聚合按下面三步推进：

1. 返回值换成 dataclass。
2. 删掉这个聚合照抄的 port。`storage/assembly.py` 直接标注具体的 repository 类型，调用方继续用自己的窄 Protocol（Protocol 是结构类型）。"storage 之外不许 import repository 实现"这条规则保持不变。
3. 测试 fake 改成按窄 Protocol `create_autospec`。

`ports/external/compute_provider.py` 的 `ComputeProvider` 是保留的范例：它真有两个实现，值也都是具体类型。

另有 5 个 admin 查询类在 `storage/` 根目录下（约 1,800 行），没有 port，api 直接 import 它们来拿异常类型。处理办法：

- 异常移到 `ports/admin_queries.py`；
- 这 5 个类移到 `repositories/`；
- `test_storage_boundary.py` 的检查范围扩大到整个 `storage/`。

### 7.4 共享的 fence

**attempt 租约。** `_sql/attempts.py:28` 已经有共用的 `lock_live_attempt`，它的 docstring 写明了为什么要先加锁再读时钟，但只有 2 个模块在用。另外还有两类自己写的检查：

- `sandbox.py:110,122` 的两步写法（`_lock_running_attempt` 用 `FOR SHARE`，再调 `_attempt_is_live`）；
- 9 个 storage 模块里共 26 处内联的 `lease_generation = $n`，锁模式和附加条件各不相同。

**residency 调用方。** 现在两个 relay 各写了一遍"这个占位凭据指名的 residency 正在服务一个活着的 attempt"：

- MCP relay 分两步：`credential_queries.resolve_residency_attempt`（`credential_queries.py:29`），然后 `resolve_model_credential`（65 行）；
- GitHub relay 合成一条语句：`GitHubRelayRepository.resolve_github_caller`（`repositories/github_relay.py:22`），docstring 写的是 "fenced as the MCP relay fences"。

两者的条件现在是一致的，但以后改其中一份时，没有任何东西会提醒改另一份。

两类 fence 都收进 `storage/_sql/fences.py`：

- `lock_live_attempt(mode=SHARE|UPDATE, host=…)`；
- `LIVE_RESIDENCY_CALLER`：一个 SQL 片段加一个函数，两个 relay 共用。以后再加 relay，也用它。

现有的那些差异哪些是有意的，还没有逐个核实。迁移时要一处一处确认，不能顺手改了语义。

## 8. 主线 D：host 端结构

### 8.1 archive 策略接口

现状：

- supervisor 构造函数接受 7 个 hook（从 `shared_session_supervisor.py:449` 开始）；
- 另有 3 个 `configure_*` 方法再绑一遍（527 行起），理由是 `create_app` 也接受外部注入的 supervisor；
- 还有 3 个公开的可变属性：`checkpoint_restore_hook`、`checkpoint_seq_for`、`environment_snapshot_hook`。

`sandbox_manager.py:1469` 起用 `getattr` 探测这些方法，然后接成一个双向环：

- `checkpoint_capture.start_seq_for = supervisor.start_seq_for`（1517 行）；
- `supervisor.checkpoint_seq_for = checkpoint_capture.receipt_start_seq`（1520 行）。

改成一个接口：

```python
class GroupArchivePolicy(Protocol):
    def prepare_start(self, ...) -> ...: ...
    def started(self, ...) -> ...: ...
    def prepare_final(self, ...) -> ...: ...
    def invalidate_final(self, ...) -> ...: ...
    def markerless_final(self, ...) -> ...: ...
    def stopped(self, ...) -> ...: ...
    def restore_checkpoint(self, ...) -> ...: ...

class NullArchivePolicy: ...   # 没有配置 archive 时用
```

- 策略在构造时传一次。`configure_*` 方法、三个可变属性和 `getattr` 探测全部删掉。
- 双向环换成一个双方共同依赖的 `StartSeqLedger` 对象，谁也不再持有对方的方法。

这一步成本最低，又是拆 supervisor 的前提。

### 8.2 sandbox manager 的 HTTP 层

`create_app` 是一个约 1,900 行的闭包，同一个操作要写 3–4 遍：

- `SessionSupervisorClient` 里的方法；
- pydantic 请求模型；
- 路由闭包；
- supervisor 里的方法。

拆法：

- 新建 `cloud_agent_host/manager_api/models.py`，放请求和响应模型，server 和客户端 import 同一份。
- `manager_api/` 下按主题拆 `APIRouter`：shared sessions、删除和 archive、工作区文件、终端、checkpoint、设置。
- `create_app` 只做组装，目标在 150 行以内。
- `_TerminalManager`（663 行）移到独立模块。
- 删掉路由里探测测试 fake 的写法，例如 2306 行的 `getattr(supervisor, "take_reported_checkpoint")`，让 fake 实现完整接口。
- 删掉模块 docstring 里的 "public gateway"（第 3–4 行）。

同一个服务现在有三个名字：线上 ID 叫 "broker"（`sandbox_manager_compat.py`），文档叫 "Sandbox Manager"，客户端叫 `SessionSupervisorClient`。统一叫 Sandbox Manager，客户端改名 `SandboxManagerClient`。"broker" 只留在 compat 别名里，2026-12-31 到期时一起删掉。

### 8.3 `ExactContainer`

`chat_sandbox.py:412` 的 `ChatSandboxManager` 约 3,100 行。从它拆出去的模块反过来调用它的私有方法：

- `chat_sandbox_exec` 调 `_proven` 和 `_require`；
- `chat_sandbox_writers` 调 `_require_exact`；
- `chat_sandbox_markers`、`tree_copy` 和 `mcp_helpers` 也一样；
- `chat_sandbox_network.py:161` 的 `ChatNetworkContainers` Protocol 直接把 `_run`、`_require`、`_require_exact`、`_verify_identity`、`_kata_privileged_exec` 写成了接口。

这几个私有方法其实就是真正的接口。把它们提成公开组件 `cloud_agent_host/exact_container.py`：

```python
class ExactContainer:
    """一个身份已被证明的容器：校验身份、有界 exec、fail-stop。"""
    def require(self, identity: ContainerIdentity) -> ProvenContainer: ...
    def run(self, proven: ProvenContainer, argv, *, limits) -> ExecResult: ...
    def privileged_exec(self, proven: ProvenContainer, ...) -> ExecResult: ...
```

network、markers、writers、installers、guest observer，以及 #1103 的 `github_relay_trust`（经由 `chat_sandbox_exec`）都改为依赖它。`ChatSandboxManager` 退化成管理 group 和成员的一层外观。

supervisor 有同样的问题：`environment_ready_boot.py:43–50` 和 `sandbox_deletion_diagnostics.py:178–204` 直接读写 `supervisor._lock`、`_member`、`_members`、`_manager`。这部分在 8.4 里解决。

加一个测试：`cloud_agent_host` 里不许跨模块访问别的类的下划线属性。以现状为基线，只降不升。

### 8.4 拆 supervisor

前提：A1 和 8.1 已经完成。拆分只依赖 `GroupLifecycle` 的接口，不依赖 A2 换掉底层表示。拆成三部分：

- **`GroupLifecycleController`**：负责 start、stop、reconcile、orphan 恢复和 archive 编排，持有 `GroupLifecycle`。最大的几个方法都归这里：`_reconcile_checked_group`（1372 行起）、`capture_initial_archive`（3561 行起）、`_stop_checked_group`（4112 行起）。
- **`MemberSessions`**：负责成员租约、轮次、residency 和原生会话。
- **几个服务**：`ForkService`、`EnvironmentPreparation`、`WindowSampler`。它们通过一个窄的预留接口使用 group，不再直接碰 `_lock` 和 `_members`：

```python
with lifecycle.reserve(group_id, Operation.MUTATION) as reservation:
    ...  # 预留期间 group 的身份已重新校验；退出时自动释放
```

code-structure.md 现在写着"停止、orphan 恢复、删除 archive 之前先预留生命周期，然后重新校验身份"，这条规则靠每个调用方自觉遵守。拆完以后，它就是 `reserve` 自己的行为。

### 8.5 runner：拆分，并收窄对容器的访问

现在一轮的执行路径是：

1. host service 的 `run_forever`（`runner.py:3588`）把这一轮交给一个每轮单独起的子进程。
2. 子进程依次进入 `_serve_turn`、`_run_turn`、`_run_shared_live_attempt`（2019 行起，约 1,000 行，带复杂度豁免）。
3. 子进程对 Sandbox Manager 发大约 9 次 HTTP 请求，其中包括 NDJSON 的轮次流。
4. Sandbox Manager 里再经过 supervisor、`ResidentTransport`、drivers，最后到运行时的 dialect。

与此同时，子进程自己也构造了一个 `ChatSandboxManager`（2061 行）和一个 `RetainedChatBackend`（2398 行），直接 exec 进同一个容器去读 git head、导入 base（`_import_retained_base`、`_retained_head`，1656–1709 行）。于是两个进程在操作同一个容器。两个进程都挂着 Docker socket，所以这层 HTTP 拆分也算不上权限边界。

我推断拆成两个进程的真实原因是：常驻 CLI 要比每轮的子进程活得久，而且控制面也要直接调 Sandbox Manager。这一点要先确认（第 15 节）。

改法：

- **runner 子进程只拿一个窄的容器句柄。** 它不再构造完整的 `ChatSandboxManager`，只拿 8.3 的 `ExactContainer`：身份已证明的 exec，加上 `import_base_bundle`。读 git head、导入 base 仍由 runner 自己做。
  - **不把 base 导入搬回 Manager。** 11-04 那一版写的是"改成 Manager 的端点"，但 #1103 刚删掉 Manager 的 `/import-base` 端点和它的 bundle 流式传输（`base_bundle_request.py`、`base_bundle_stream.py`）。base 要在 sandbox 外用 `clone_token` 克隆，再以 bundle 形式导入；搬回 Manager，就得把那套流式传输重新加回来，或者把克隆凭据交给 Manager。两种都不划算。
  - 目标因此从"容器只有一个 owner"收窄为"容器只有一个身份校验入口"：两个进程都可以 exec，但都只能经过 `ExactContainer`。
- **`runner.py` 拆成三个模块：**
  - `host_service.py`：claim 循环、host 心跳、drain。
  - `turn_child.py`：一轮的执行。live attempt 写成一串显式的阶段，每个阶段一个函数，失败处理放在阶段之间。
  - `source_preparation.py`：克隆、worktree、base，也就是现在 554–984 行的 `_run_git`、`prepare_worktree` 等。git 相关的三份实现（这里、`workspace_git.run_git`、`sandbox_manager._run_git`）合并成一份。
- **`environment_tests.py` 不再延迟 import runner 的私有名字**（73、243、387、429 行）。共用的部分放进上面这三个模块。

### 8.6 每个 CLI 一个包

同一个 CLI 的知识现在分散在至少四个地方：

- `drivers.py` 有 `CodexAppServerSession`（1138 行起）、`PiRpcSession`（1630 行起）、`HttpAgentLiveSession`（1809 行起），以及 Claude 的会话类。Codex 和 HTTP 系 agent 的事件解析也写在这里。
- `runtimes/` 里只有 Claude 和 pi 的事件解析。
- `ResidentTransportFactory`（`resident_transport.py:460`）有 `_prepare_claude_member_state`、`_codex_member_state_argv`、`fork_native`、`_fork_claude`、`_recover_codex_fork` 等。
- 顶层还有 `codex_app_server`、`codex_mcp`、`codex_plugins`、`kilo_mcp`、`http_bridge_parts`。

改成每个 CLI 一个 `runtimes/<cli>/` 包，里面放它的 dialect、live session、成员状态、Fork、MCP 配置，以及 6.2 说的 facet 接线。`drivers.py` 只保留通用契约。

### 8.7 host 包分组

`cloud_agent_host/` 下 116 个模块全部平铺，按下面分组（纯移动）：

| 子包 | 内容 |
|---|---|
| `launch/` | 6.2 的 facet；`sandbox_browser`、`sandbox_mcp`、`github_relay_env`、`github_relay_trust`、`platform_instructions`、`resident_launch_config` |
| `member_setup/` | `plugins`、`other_plugins`、`codex_plugins`、`mcp_helpers`、`kilo_mcp`、`runtime_install`、`compat_helper`、`member_preparation`、`tree_copy`、`install_locks` |
| `archive/` | `sandbox_archive*`、`sandbox_checkpoint*`、`sandbox_deletion_*` |
| `observe/` | `guest_observer*`、`member_resources`、`*_window_census`、`resources`、`sandbox_resources`、`tool_windows`、`host_capacity` |
| `network/` | `network_guard`、`chat_sandbox_network`、`chat_network_disconnect`、`guarded_workloads`、`idle_host_rules` |
| `service/` | `host_service`、`host_runtime`、`host_control`、`turn_launch`、`enrollment`、`runner_stop` |
| `manager_api/` | 见 8.2 |

"receipt 加 flock 锁文件"这套写法出现了三遍：`environment_preparation.py:190`、`action_receipts.py:53`、`upload_import.py:86`。合成一份，放在 `archive/receipts.py`。

## 9. relay 的位置

#1103 以后，sandbox 外面有两个持有凭据、替 sandbox 发请求的 relay：MCP relay 和 GitHub relay。sandbox 里还有 #1097 的 MCP gateway、OpenCode/Kilo 的 HTTP bridge、协议翻译器和浏览器服务。它们的形状相同，都是"验证 bearer、解析调用方、按策略决定、转发"。

**不合并 listener 和隧道。** 一个 relay 一个 listener、一条隧道、一种 token 前缀，这是有意的隔离：一把能开两条隧道的钥匙，会让隔离变成摆设（见 #1103 spec §4.1）。这次只统一代码：

- 调用方解析共用 7.4 的 `LIVE_RESIDENCY_CALLER`。
- 新的平铺模块 `github_relay_policy.py`、`git_identity.py` 移进 `source_control/`（11.1）；`api/github_relay.py` 和 `api/mcp_relay.py` 保持原位，它们是 HTTP 适配层。
- 占位凭据的前缀和签发已经集中在 `tokens.py`，保持不变。在第 4 节的术语表里列出全部前缀。

## 10. 主线 E：host 协议 v2

这一节只给方向，不是可以直接照着实现的设计。它改动的是通信、调度和 fence 语义，不只是代码整理，所以：

- 每一项实现前都要另写 spec；
- 第一个 PR 不是协议改动，而是一个混合版本测试（10.0）。

### 10.0 先补混合版本测试

现在 CI 里的 `scripts/check_rolling_upgrade.py` 验证的是"上一版的代码还能用升级后的数据库"：它让上一版的 worker 进程在迁移过程中保持运行。它不验证上一版的 host（runner 加 Sandbox Manager）能否和本版的控制面通信，也不验证反过来。11-04 那一版把它当成协议兼容的验证，这是错的。

在任何协议改动之前，先加一个 CI 任务，两个方向各跑一遍：

- 上一个发布 tag 的 host 镜像，对本版控制面；
- 本版 host，对上一版控制面。

两个方向都走一遍 claim、一轮执行、心跳、停止。没有它，10.1–10.6 都无法验证。

### 10.1 协议版本

现在线上协议没有版本号。版本差异靠逐个字段的容忍来处理：

- 10 个 `COMPAT(remove-after=…)` 标记；
- 约 39 处没有标记的"旧版本不带这个字段"写法，例如 `ClaimedJob.from_response` 里的 `body.get(...) or default`、各种 `_drop_unusable_*` 校验器、`/worker/wait` 返回 404 时退回轮询。

改法：

- host 注册、host 心跳和 claim 请求都带上 `protocol_version: int`。控制面在心跳响应里给出自己支持的 `[min, max]`；版本不在范围内的 host 进入 drain，并记下原因。
- 兼容代码只放在两个模块：`cloud_agent/api/worker_compat.py`（处理老 host 的请求）和 `cloud_agent_host/control_plane_compat.py`（处理老控制面的响应），都标 `COMPAT`。
- 那些没有标记的容忍写法，碰到相应文件时就迁进这两个模块。

### 10.2 统一的成员 fence

现在 `/worker/jobs/{id}/sandbox/*` 下的各个路由，各自带一组不同的 fence 字段：

| 请求 | 带的 fence |
|---|---|
| `running` | sandbox、activation |
| `admit` | sandbox、activation、residency |
| `settle` | sandbox、activation、residency |
| `process` | sandbox、activation、residency、process |
| `abandon` | sandbox、activation |

另外，`residency` 请求收下 `runtime_id` 和 `runtime_version` 后直接丢掉（`repositories/residency.py:66` 的 `del runtime_id, runtime_version`）。

改成一个统一的模型：

```python
class MemberFence(BaseModel):
    sandbox_id: str
    sandbox_generation: int
    activation_generation: int
    residency_generation: int | None = None
    process_generation: int | None = None
```

- 每个 sandbox 请求都带 `fence: MemberFence`。服务端按这一次转移需要的分量校验，校验函数和 7.4 的租约 fence 一起放在 `_sql/fences.py`。
- 旧的分散字段在一个版本的兼容期内继续接受，由 `worker_compat.py` 转换成 `MemberFence`。
- **不把这些路由合并成一个 `POST /sandbox/transition`。** 分开的路由各有自己的鉴权、请求体大小限制和日志名，合并换不来什么。

### 10.3 凭据封装

#1103 和 #1104 之后，一次 claim 和 residency 一共下发这些凭据：

| 凭据 | 线上位置 | 谁用 | 现在脱敏吗 |
|---|---|---|---|
| `worker_token` | claim | runner | 不进 sandbox |
| `mcp_token` | claim | 没人用（弃用，2026-11-30 到期） | 不进 sandbox |
| `clone_token` | claim | runner 克隆 | 不进 sandbox |
| `model_connection.api_key` | claim | 成员 CLI | 是 |
| `model_connection.local_token` | claim | 成员 CLI，翻译方案 | 是 |
| owner key（`credential`） | residency | 成员 CLI | 和 claim 那份是同一个值 |
| SSH 密钥 | claim | 成员 tmpfs | 是 |
| 环境 Secrets | claim | 成员环境 | 是 |
| `mcp_credential`（`amr.`） | residency | 成员 CLI，经 MCP relay | 否 |
| `github_credential`（`agh.`） | residency | 成员的 git/gh，以 `GH_TOKEN` 注入（`github_relay_env.py:65`），经 GitHub relay | 否 |
| `SandboxMcpServer.secret_env` | claim（#1104） | slot runner | **否**（见 2.2） |

问题有三个：

- **重复下发。** 同一个 owner key 在 claim 和 residency 里各给一次，host 还得核对两份的 revision 和 plan 是否一致（`claimed_job.py:217` 的 `require_residency_connection`）。
- **使用方有四类**：runner、成员 CLI、slot runner、relay 占位，但线上并没有标明某个凭据属于哪一类。
- **哪些要脱敏，没有写在任何地方。** 两个占位凭据不脱敏，可能是有意的（它们只在 residency 活着、对应 relay 内才有用），但没有任何地方记下这个决定。

改成每个凭据一个条目：

```python
class DeliveredCredential(BaseModel):
    kind: Literal["worker", "clone", "model_key", "translator_token", "ssh_key",
                  "env_secret", "mcp_relay", "github_relay", "sandbox_mcp_secret"]
    audience: Literal["runner", "member", "slot_runner"]
    name: str                 # 环境变量名，或私有目录里的文件名
    value: SecretStr
    mask: bool                # 是否从持久化输出里遮掉；每种 kind 固定，写在表里
    revision: int | None = None
    expires_at: datetime | None = None
```

- claim 和 residency 各带一个 `credentials: list[DeliveredCredential]`。runner 收到后，按 `audience` 交给对应的 facet（6.2）。
- `mask` 由 `kind` 决定，写成一张表，并有测试保证每个 `kind` 在表里都有一行。这样，占位凭据要不要脱敏，就成了写下来、经过评审的决定。我的建议是也脱敏：成本为零，又能防止 residency 还活着时，凭据从事件日志里被别人看到。
- 投递时机保持不变。SSH 密钥和环境 Secrets 仍然只随本轮的 claim 下发，"一个成员只在自己这一轮开始时拿到 key"这条性质不变。owner key 是否只下发一次，见第 15 节。

### 10.4 控制面到 host 的命令队列

控制面通知 host 现在至少有六条路径：

1. claim，加上 `/worker/wait` 的唤醒 token；
2. 每个 attempt 的心跳响应，只带一个 bit：`cancel_requested`；
3. host 心跳响应：slots、设置、archive、下载、`residencies_lost`、stop 命令；
4. 单独的 profiling 策略轮询；
5. 环境运行和快照删除的轮询；
6. `sandbox_manager_client.py` 里 35 个直接调用 Sandbox Manager 的方法。其中 send-now 有自己的 60 秒数据库租约（`application/send_now.py:42`）；residency 丢失的通知由三个调用点直接推送（`application/residency.py:70`、`workers/attempts.py:154,205`），同时 host 心跳里还会再回显一遍。

终端挂起还会绕一圈：host 先调控制面，控制面再回调 host（`api/worker.py:1599` 起）。

目标：把 host 心跳里已有的 stop 命令推广成一个持久化的命令队列。

- 新表 `agent_host_commands(host_id, seq, kind, target, payload, fence, created_at, outcome, settled_at)`。
- 命令随 host 心跳响应和 `/worker/wait` 响应下发。
- 直接 HTTP 推送保留，但只用来"踢一下"、降低延迟；正确性只依赖队列。推送失败不重试，下一次心跳会带上。

在写详细 spec 之前，必须先回答下面几条。11-04 那一版写的是"回报已处理的最大 `seq`"，这几条它都没有回答：

- **ack 按条，不累计。** "已处理到第 N 条"会把中间失败的命令一起确认掉。如果失败的是一条撤销命令（例如 residency 丢失后的拆除），就等于默默丢掉了撤销。所以 host 要逐条回报结果：`applied`、`stale`（fence 已经过期，不需要再做）、`failed_retryable`、`failed_terminal`。只有前两种算完成，`failed_retryable` 会重新下发。
- **幂等靠效果，不靠序号。** `seq` 唯一只能防止同一条命令被记两遍。host 在执行完、回报之前崩溃，命令就会被再下发一次，这时只有"执行两次和执行一次效果相同"才安全。所以每种 `kind` 都要说明自己在重放时的行为，并以 `(kind, target, fence)` 判断是否已经做过：例如 fence 已经过期，就回报 `stale`。
- **同一个目标上的顺序。** 对同一个 `target`（group 或成员）的命令按 `seq` 依次执行。较新的 fence 让较旧的命令变成 `stale`，不能反过来。
- **永远做不成的命令。** `failed_terminal` 和长期 `failed_retryable` 的命令要有告警和人工处理路径，不能永远留在队列里。
- 迁移顺序：先迁 residency 丢失，它现在有三个推送点加一次回显，最乱；然后是 profiling 策略，再是取消；最后再看终端挂起和环境运行值不值得迁。send-now 涉及一轮的交接，不在第一版里。

### 10.5 能力匹配

能力信息现在有四个来源：

- host 行上的能力，整台机器的；
- 每次 claim 的 attestation，每个 replica 的；
- 实时查询 Sandbox Manager 的 `/capabilities`；
- 这一轮的 `requires` 元数据。

环境、SSH、模型连接的检查，是在 claim **之后**才做的，不满足就 `release_claim(retry_after_seconds=30)`。也就是说，不具备能力的 host 会先领走这一轮，过 30 秒再还回来。

这里要分清两类检查：

- **能力**：这台 host 能不能做这件事，例如支持环境、SSH、restore、某个协议版本。它是静态的，和用户无关。
- **准入**：这件事现在还能不能做，例如 owner 是否仍有权限、环境绑定的 access generation 是否仍有效、团队成员是否已离开、模型连接的 revision 和 plan、SSH 服务器是否仍启用。它是动态的，随时可能撤销。

改法只针对能力：

- 入队时把这一轮需要的能力算全，写进 `requires`。claim 的 SQL 拿它和本次 attestation 匹配，没有这些能力的 host 根本领不到这一轮，也就不会"先领走、30 秒后还回来"。
- host 行上的能力只用于管理界面显示和容量估算。
- 能力字符串统一成 kebab-case（现在 `goal_report_v1` 的写法和别的不一致），保留一个版本的别名。

**准入检查仍然在 claim 时做，一项都不挪。** AGENTS.md §7 要求每一次把值交给容器的读取都重新检查准入（"each turn's claim, terminals, actions, Fork…"），提前到入队时做就会漏掉撤销。11-04 那一版写的是"把环境、SSH、模型连接的检查挪到 claim 之前"，把两类检查混在了一起。

### 10.6 成员状态只往一个方向复制

- **claim。** `WorkerThreadSandbox` 把调用方成员 15 个字段里的 12 个再抄一遍，然后在 `group_members` 里又列一次同一个成员。改成 claim 只带 group 和 `group_members`，调用方按 `thread_id` 自己找。旧字段在一个版本的兼容期内继续发送。
- **心跳。** 心跳会把每个成员的全部字段发回来（`RetainedSandboxBeat.members`，`schemas/hosts.py:208`），其中有不少是数据库自己拥有的，例如 `admitted_job_id`、`last_settled_job_id`、`forked_from_thread_id`。可以只报 host 观察到的事实，**但代际字段必须保留**：
  - `_fence_refuses`（`_sql/sandbox_observations.py:149`）按 sandbox 的 generation 和容器 id 拒收迟到的报告；
  - `_member_moves`（162 行）按 activation、process、residency 三个 generation 判断这份报告是不是比数据库里的更新。

  去掉它们，一份迟到的旧报告就可能覆盖新进程的状态。所以每个成员的报告保留 `thread_id`、三个 generation 和 `resident_state`，只去掉 reconcile 不读的那些字段。到底去掉哪些，以 `_REPORTED_MEMBERS_SQL` 实际读取的列为准，写进 spec 后再删。11-04 那一版写的是"只报容器状态、进程状态、原生会话 id"，会把这些 fence 一起删掉。

### 10.7 顺手清理

- `shared_live: Literal[True]` 是只能为 `True` 的字段，出现在 4 个模型里。按 10.1 的方式删掉。
- wire 文档里还残留 Gateway 的说法：`schemas/workers.py:254`（"absent for a Gateway turn"）和 `schemas/hosts.py:462`（"A Gateway grant"）。

## 11. 主线 F：模块整理和残留

### 11.1 控制面顶层的放置规则

`cloud_agent/` 顶层有 82 个平铺模块，没有放置规则：code-structure.md 只提到 "application/ and services"，却没定义什么是 service。#1103 又新增了两个平铺模块：`github_relay_policy.py` 和 `git_identity.py`。

**规则：** 顶层只留 `app.py`、`settings`、`logging_utils`、`tokens` 和功能包；其他模块都归到某一层，或归进一个功能包，做法和现在的 `goals/`、`accounts/`、`file_activity/` 一样。这条规则写进 code-structure.md，并加一个测试：顶层模块数只降不升。

纯移动，每行一个 PR：

| 目标 | 内容 |
|---|---|
| `context/` | 9 个 `context_*` 模块 |
| `usage/` | `runtime_usage`、`ledger`、`admin_usage`、`grant_usage`、`owner_usage`、`usage_batches`、`usage_range`。`grant_usage` 改名为 `usage/sums.py`，它和 grant 已经无关；`ledger` 改名为 `usage/turn_report.py`，它记的是一轮的 token 报告，不是账本 |
| `measurement/` | `measurement_*`、`operation_measurement` |
| `sandbox_manager/` | `sandbox_manager_client`、`_routing`、`_pool`。测试里按字符串 patch `sandbox_manager_routing` 的地方要一起改 |
| `source_control/` | `github_app`、`github_cache`、`github_user_token`、`source_control`、`source_control_runtime`、`entitlement`、`github_relay_policy`、`git_identity` |
| `compute/` 或 `providers/compute/` | `compute_config`、`compute_registry`、`compute_secrets` |
| `schemas/` | `schemas_compute.py` 改名为 `schemas/compute.py`；`owner_usage` 和 `context_insights` 里的模型；`admin_conversations`、`admin_sandboxes`、`admin_sandbox_inventory`、`admin_database`、`admin_observability` 里的 Pydantic 模型 |
| `domain/` | `host_pool.py` 的状态机移到 `domain/host_pool.py`；`REPO_PATTERN` 和 `is_ref_name` 移到 `domain/source_control.py`，这样 `schemas/` 不用再 import 一个 1,400 行的 GitHub 客户端；`admin_observability` 里的 cursor 编解码移到 `domain/cursors.py`，owner 侧的功能也在用它 |

`ports/` 现在会 import 一些顶层平铺模块，没有测试管这件事。上面的移动完成后，`test_application_boundaries.py` 加一条：ports/ 只能 import domain/、schemas/ 和 `cloud_agent_shared`。

`settings.py`（1,247 行）同时装着注册表、加密存储和 getter，可以拆成一个 `settings/` 包。优先级低，碰到时再做。

### 11.2 删除没有运行时的 MCP location

#1104 合并后，MCP connection 只剩两种 location 能运行：`remote`（服务）和 `sandbox`（program）。剩下两种没有运行时：

- **`managed-worker`。** `mcp/launch.py` 仍会为它构建启动计划（284 行起），`mcp/host.py` 再拒绝运行它。
- **`user-connector`。** 它背后有自己的表（`agent_mcp_connectors`、`agent_mcp_connector_connections`，来自迁移 0008）、`connector_id` 字段，以及 `storage/repositories/mcp.py`、`api/mcp_connections.py` 里的专门分支。但 connector 的运行时从未发布（`operations.md` § *Connector configuration*）。

按删除 publisher 的方式分三步走：

1. **迁走存量数据。** #1104 把"已存为 `user-connector` 的 connection 迁到 sandbox"列成了后续工作。这一步先做：能按 program 规则重新解释的（单一钉死的 `npx`/`uvx` 包），改成 `sandbox`；其余的停用，并在界面上提示用户重新添加。
2. **删代码。** 删掉 `ExecutionLocation.MANAGED_WORKER`、`USER_CONNECTOR` 及其全部分支，导入草稿的 `DraftLocation` 只剩两个值，并删掉 connector 的 API。
3. **删表。** 下一版用单独的迁移删除两张 connector 表和 `connector_id` 列，理由同 #1103 spec §9：滚动升级期间，上一版仍会读它们。

### 11.3 Gateway 和 grant 的残留

- `context_timing.py` 的 `ThreadTiming` 里有 `gateway`、`gateway_reason` 等字段。它唯一的调用点（`api/context.py:126`）传入的是空 pages 和 `gateway_started=0`，所以 `gateway` 永远是 `"not_recorded"`，按请求关联的那段代码从不执行。这些字段还留在 `openapi.json` 和前端的 `TimingStats.tsx` 里。删除属于线上契约改动：先让前端停止读取，再从 API 里删掉。
- 还写着 Gateway 的 docstring：`sandbox_manager.py:3`、`sandbox_manager_client.py:1`、`terminal_coordination.py:1`、`usage_range.py:4`、`owner_usage.py:11`。
- AGENTS.md §2 的表还列着 `grant_usage.py`。
- `cloud_agent_shared/protocol_compat/`（约 3,150 行，Messages/Responses 和 Chat Completions 之间的翻译）是一个算法库，不是 `cloud_agent_shared` 自称的"共享词汇"。两边确实都在用，所以留在原地，只改包的说明。

### 11.4 前端

优先级最低，只记方向，不排进第 13 节：

- `components/features/agents/` 下平铺着 246 个文件。按 `conversation/`、`composer/`、`sidebar/`、`workspace/`、`trajectory/` 分组，做法照已有的 `job-detail/` 和 `mcp/`。
- `adapt.ts`（1,509 行）同时适配 Job 和 Thread 两种 API 形状。文件头注释说 UI 是照着一个比真实 API 更丰富的 mock 设计的。拆成按 API 形状分开的两个 adapter，并删掉 API 从来不提供的字段。

## 12. 不做的事

- **不给线上字段和数据库改名。** `thread_id`、`job_id`、`agent_jobs` 都不动。改名只换来可读性，代价却是一整轮兼容期。有第 4 节的术语表就够了。
- **不把 attempt 心跳并进 host 心跳。** attempt 租约是防止重复执行、重复开 PR 的 fence 核心；合并换来的只是少一个请求。
- **不合并 sandbox 的那几个路由**（10.2）。
- **不合并 relay 的 listener 和隧道**（第 9 节）。
- **不合并三套 S3 记录**（导出 archive、checkpoint、删除前备份）。这涉及恢复语义，不是结构问题；要做的话另写 spec。
- **不一次性删掉 ports 层**（7.3）。
- **不引入依赖注入框架**，也不追求"application 与 HTTP 框架无关"之外的分层纯度。

## 13. 交付顺序

每一项是一个 PR，除非另有说明。标"行为不变"的 PR，测试只改 import 和 patch 路径。

**现在，放在 #1104 里**

- B0：把 `secret_env` 加进 `claim_secret_values`，更新 `claimed_job.py:99` 的注释（6.3）。

**第 0 阶段：一个 PR**

- AGENTS.md §6 的轮次定义，加上第 4 节的术语表；
- 11.3 里只改 docstring 的部分，以及 AGENTS.md 的表；
- 把 `grant_usage` 移到 `usage/sums.py`。

### 13.1 承诺要做的：行为不变，局部改动

**第 1 阶段：成本低、回报高**

1. B1：`Secret` 类型，脱敏集合从 claim 推出。
2. 8.1：`GroupArchivePolicy`。
3. 7.1 前半：`domain/errors.py`，先迁 `job_admission` 和 `pending_messages`。
4. 7.1 前半的后续：迁剩下三个模块，加上"application 不许 import fastapi"的测试。
5. 7.1 后半：`StorageConflict` 和 `ConflictCode`，先迁 compute。
6. 5.1 的 A1：`GroupLifecycle` 外观层。
7. 5.2：`domain/sandbox_group.py`、SQL 常量和字面量棘轮。
8. 7.4 的 residency 半边：两个 relay 共用 `LIVE_RESIDENCY_CALLER`。

**第 2 阶段：结构调整**

9. B2、B3：`LaunchFacet`，以及把准备步骤搬进 facet。
10. 7.2：`application/turn_claim.py`。
11. 8.2：sandbox manager 拆 router，请求模型由 server 和客户端共用。
12. 8.3：`ExactContainer`，加下划线访问棘轮。
13. 8.4：拆 supervisor，基于 A1 的接口。
14. 8.5：拆 runner，子进程改用 `ExactContainer`。在 staging 的 Kata host 上验收。
15. 7.4 的租约半边：收拢租约 fence。

第 1、2 阶段做完，"为什么现在做"（第 2 节）里的大部分问题就已经解决：新加能力只改三处，错误不再绑在 HTTP 上，几个上帝对象拆开。

### 13.2 先写设计、评审后再排期

下面这些不在本文里给出实现设计。每一项都要先写 spec，评审通过后才排期。

- **10.0 混合版本测试**：是 E 里所有改动的前提。它本身不改协议，可以先做。
- **A2**：换掉 host group 状态的底层表示（5.1）。
- **E 的其余各项**：10.1 协议版本、10.2 `MemberFence`、10.3 凭据封装、10.5 能力匹配、10.6 成员状态、10.4 命令队列。这里排的是建议的先后顺序；10.4 和 10.6 风险最高，放最后。

### 13.3 穿插进行

**删除和纯移动**

- 11.2：删除没有运行时的 MCP location（共三个 PR）；
- 11.1：控制面的移动，每个目标一个 PR；
- 8.7：host 子包；
- 8.6：每个 CLI 一个包。

如果某块代码正在做结构 PR，就不对它做移动，免得冲突。

**随聚合推进**

- 7.3：返回值换成 dataclass，删掉照抄的 port，每个聚合一个 PR。

## 14. 验证

**每个 PR：**

- 按 CLAUDE.md 跑完整的本地门禁；
- 碰到 storage 的，跑 `make test-db-local`；
- 碰到 `docs/developer/` 的，跑 `make docs-verify` 并更新译文；
- 模块变小时，降低 `test_file_size_budget.py` 里对应的条目；函数降回限额以下时，删掉它的 `noqa`。

**13.1 第 2 阶段的 host PR**：每个都在 staging 的 Kata host 上走一遍新建对话、续接、Fork、停止和恢复、管理员删除。B2/B3 还要额外验证：改一个 sandbox MCP 服务器、改 GitHub 仓库、改模型连接，各自都只让下一轮起新进程，原生会话照常续接。

**13.2 里协议改动的每个 PR**：用 10.0 的混合版本测试，两个方向各跑一遍。`scripts/check_rolling_upgrade.py` 仍然要通过，但它只覆盖数据库迁移。

**完成的标志**：下表这些数字都变成测试里的棘轮。

| 指标 | 基线 | 目标 |
|---|---|---|
| 超过 1,500 行的后端模块 | 17 | 第 2 阶段后不超过 8 |
| `noqa … #771` | 100 | 第 2 阶段后不超过 50 |
| application/ 里的 `HTTPException` | 51 | 0 |
| `str(exc) ==` | 7 | 0 |
| storage 里内联的 group 状态字面量 | 52 | 0 |
| 写 `agent_thread_sandbox_memberships` 的 storage 模块 | 11 | 1 |
| supervisor 里的 `SessionBusy(` | 54 | 只在 `group_lifecycle.py` 里 |
| 新加一种 sandbox 能力要改的地方 | 5 | 3（facet、线上字段、CLI 适配器） |
| 手写的脱敏或兼容性名单 | 2 | 0 |
| 控制面顶层模块 | 82 | 不超过 10 |
| 未标记的版本容忍写法 | 约 39 | 0，全部进 compat 模块 |

## 15. 待确认的问题

- **A2 的范围。** 进行中的操作是否至多一个，`container_lost` 能和哪些状态并存，都要在 A2 的 spec 里从 40 处赋值逐一推出来。如果推出的组合太多，A2 可以只做到"判断集中、表示不变"为止。
- **一轮为什么要跨两个进程？** 8.5 推断的原因是常驻 CLI 要比每轮的子进程活得久，控制面也要调 Sandbox Manager。动手之前要和写这部分代码的人确认，有没有崩溃隔离、内存方面的其他原因。
- **owner key 能不能只下发一次？** 只在 residency 里下发（claim 只带 revision 和 plan），就能去掉"两份必须一致"的核对。但 residency 可能在没有新 claim 的情况下重建，这时 key 由谁负责脱敏要先确认。确认之前，10.3 保持两次下发。
- **占位凭据要不要脱敏？** 10.3 建议脱敏，需要写 MCP relay 和 GitHub relay 的人同意。
- **facet 和 CLI 适配器的边界。** 平台说明、MCP 接线这类按 CLI 分支的逻辑放进适配器，是 6.2 的决定。B3 实现时，如果发现某个 facet 的 `env()` 本身就要随 CLI 变化，就让 facet 提供数据、适配器负责写入，不要在 facet 里写 `runtime_id` 分支。
- **存量的 `user-connector` connection 怎么处理。** 11.2 第 1 步的自动改写规则，要和 #1104 列出的后续工作对齐，并决定由谁来做。
- **命令队列的成本。** 每条命令都要写一行、ack 一次。按现在的推送频率估计写入量很小，但 10.4 的 spec 要给出实测数字。

## 16. 评审后的修订

Codex 评审了 11-04 那一版（`docs/agents/reviews/2026-11-05-structural-refactor-review.zh-CN.md`，不在本仓库）。下面是这一版逐条核实后的处理：

| 评审意见 | 核实结果 | 本文的修改 |
|---|---|---|
| `starting` 和 `start_failed` 会同时成立 | 成立：重试在 1127 行进入启动，1265 行才重写 `start_failed` | 5.1 改成四类，`start_failed` 归为记录；A2 改为另写 spec，不再以"staging 一周没报警"为依据 |
| `check_rolling_upgrade.py` 不验证新旧 host 和控制面之间的通信 | 成立：它只让上一版的 worker 连接迁移后的数据库 | 新增 10.0 混合版本测试，作为 E 所有改动的前提；14 节改了验证方式 |
| 心跳里的代际字段用来拒收迟到的报告，不能删 | 成立：`_fence_refuses` 和 `_member_moves` 都依赖它们 | 10.6 改成保留三个 generation，只删 reconcile 不读的字段 |
| 命令队列没有失败和重放规则 | 成立：累计 ack 会把失败的撤销命令一起确认掉 | 10.4 列出逐条 ack、按效果幂等、同一目标的顺序、做不成的命令四个前置问题 |
| #1103 的基线要重新核对 | 评审时 #1103 还是 draft，现已合并（`225e2c3fb`）；其中两点仍然成立 | 10.3 注明 `agh.` 以 `GH_TOKEN` 注入；8.5 不再计划把 base 导入搬回 #1103 刚删掉的 Manager 端点 |
| 提前匹配能力不能替代 claim 时的动态检查 | 成立：AGENTS.md §7 要求每次把值交给容器时都重新检查准入 | 10.5 区分"能力"和"准入"，只把能力匹配移进 claim 的 SQL |
| 先做局部改动，状态机替换和协议 v2 另行设计 | 同意 | 第 1 节和第 13 节改成"承诺要做的"和"先写设计再排期"两部分 |

