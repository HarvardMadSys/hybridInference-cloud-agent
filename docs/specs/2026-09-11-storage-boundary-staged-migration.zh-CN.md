# Cloud Agent 存储边界重建：分阶段实施设计

日期：2026-09-11。状态：**已实施**（阶段 1–3 全部完成，见文末《实施结果》）。
目标仓库：`HarvardMadSys/freeinference-cloud-agent`，分支 `dev`。
设计基线：`59bf2cc`（`feat(web): inspect the harness a runtime reported at init (#194)`）。

本文取代 v1/v2/v3 草稿。v3 是分阶段路线的第一次正确表述；本文保留其结构，修正其中四处与
代码不符的事实，并固化实施期做出的决定。

---

## 0. v3 的四处修正

v1/v2 把 `#177` 的目标当成了既成事实。v3 修正了这一点，但自身仍有四处描述与 dev 不符，
均在 `59bf2cc` 上重新核对：

| v3 的写法 | 实测 |
|---|---|
| `pyproject.toml` 有 `files`/`exclude`/`follow_imports`，阶段 1「把 `files` 加入 `cloud_agent`」 | dev 上**没有** `[tool.mypy]`、**没有** `make typecheck`、`ci.yml` 里**没有** mypy step。那套配置只存在于 #155 分支，且是 `ci.yml` 的 CLI flags。阶段 1 是**从零建门禁**，不是扩范围 |
| 因为 `store.py` 被 `exclude` 所以是 `Any`，「装配处的绑定不会被静态检查」 | 错误。`follow_imports = "silent"` 保留类型、只压制被导入模块的诊断；产生 `Any` 的是 `skip`。已用最小复现验证。且 `store.py` 自身只有 2 个错误，`exclude` 本无必要 |
| §2.4 列出 8 个 dbtest 文件 | 实测 **11 个** dbtest 文件 import store；v3 还列了 `test_identity_namespace.py`，它根本不 import store |
| §2.3 运行时 import 表列 7 处 | 实测 **9 处 / 9 文件**。漏了 `app.py` 的 lifespan 内 import，以及 `capacity_history.py` |

另：`bf952d2`（#177 head）不在本地克隆中，§5 的生成器草稿与一致性检查器读不到。生成器本就被
声明为「工具，不是项目」，因此只有可选的批量搬运路线受影响。

`#155`（`murphy/claude/store-ports`）已于 2026-09-11T07:07:56Z **CLOSED，未合并**。它的五个 port
用作**抽取结果的交叉验证**，不作为代码来源：其 39 个方法的成员集合与签名与本文的 port 逐字相同，
26 处方法 docstring 有差异，因为它们是手写改写的，而本文的 port 保留 store 原文。

---

## 1. 目标与不变量

**最终目标**：存储行为由窄 Protocol 与 Postgres repositories 承载；生产调用方、后台任务、两个
listener 与数据库测试全部依赖窄 port；`cloud_agent/storage/store.py`、`get_agent_job_store`、
`_require_store` 与私有 pool 穿透消失。

**不变量（任何阶段都不得改变）**：SQL 文本与绑定参数、事务所有权与锁顺序、fencing 与幂等键、
授权与 grant 清理语义、API 返回结构与错误、`agent_*` schema、产品行为。不为迁就工具恢复已删除
的列、表、状态或常量；不新增兼容 migration。

**关于「方法体零改动」的修订**：v3 的退出条件曾写作「`store.py` 的方法体零改动」。该措辞过严且
不可执行——严格类型检查要求在调用方与 store 内部做类型别名、变量命名与类型收窄修正。修订为：

> **SQL 文本、绑定参数、事务所有权、锁顺序与外部行为保持不变。**
> 类型别名、命名与收窄类修正可以做，且不必保留原有措辞。
> 任何具有真实运行时效果的修复必须**单独提交**，并验证受影响路径。

**过渡状态是合法的**，遵循三条规则：

1. **纯委托可以存在**：`store.py` 可以把某个能力转发给已迁出的 repository。委托层需要维护、
   也必须在阶段 3 清理，但它**不产生第二份业务实现**。
2. **同一时刻每个能力只有一个实现**：要么在 `store.py` 里，要么在 repository 里，不允许两边
   都保留真实 SQL。
3. **每个阶段结束时产品必须可用**：`make lint`、单元/服务层、真库层都必须是绿的。

---

## 2. 基线（`59bf2cc` 实测）

### 2.1 `cloud_agent/storage/store.py`

| 项 | 实测值 |
|---|---|
| 行数 | 8052 |
| 类 | 1 个：`AgentJobStore` |
| 公开方法 | **115**（114 业务 + `initialize`） |
| 私有方法 | 13（含 `__init__`） |
| 模块级函数 | 15 |
| 模块级常量与赋值 | 30 |
| 引用 `agent_*` 表 | 23 |

`cba9a47`（v3 的基线）是 dev 的祖先；其后的漂移中，`f9a9f5f` 与 `59bf2cc` 之间的六个提交改动了
`store.py`（+441 行）、`api/admin_hosts.py`、`api/hosts.py`、`api/admin_runs.py`、`api/routes.py`、
`app.py`、`storage/admin_queries.py`，并新增 `migrations/versions/0006_host_capacity.py`。

相对 v3 基线新增的 4 个公开方法：`host_capacity_history`、`list_capacity_history_candidates`、
`prune_capacity_history`、`record_capacity_history_point`。签名变化 2 处：

- `record_host_heartbeat` 新增 `credential_hash`、`capacity: Mapping[str, Any] | None`、
  `capacity_sample_age_ms`；
- `runner_pool` 的返回由 3 元组变为 4 元组（追加 `datetime`）。

### 2.2 迁移

`migrations/versions/`：`0001_schema_baseline` 至 `0006_host_capacity`。head = `0006`。

### 2.3 调用面

**从 `store.py` 运行时 import 的非存储模块（9 处 / 9 文件）**：

| 模块 | 导入 |
|---|---|
| `api/routes.py:162` | `RUNNING`、`SUCCEEDED`、`TERMINAL_STATES`、`request_fingerprint` |
| `api/usage.py:16`、`api/admin_usage.py:39`、`api/admin_runs.py:31` | `TERMINAL_STATES` |
| `storage/admin_queries.py:27` | `LEDGER_WINDOW_UNTIL_SQL`、`NEXT_ATTEMPT_START_SQL`、`_load_json` |
| `storage/admin_usage_queries.py:18` | `_load_json` |
| `capacity_history.py:40` | `CAPACITY_HISTORY_RETENTION_S` |
| `app.py:855` | `AgentJobStore`（lifespan 内，紧接 `AgentJobStore(pool)`） |
| `deploy_readiness.py:32` | `AgentJobStore` |

另有 15 处 `AgentJobStore` 导入在 `TYPE_CHECKING` 下，只影响类型标注。

按方法引用扫描的候选生产文件（17 个文件、119 个 (文件, 方法) 对），最大的是 `api/routes.py`（55）、
`app.py`（15）、`api/admin_hosts.py`（10）、`api/hosts.py`（9）。新增的 `capacity_history.py` 引用
3 个容量方法。

装配点：`app.py` 的 lifespan 建 pool、构造 store；第二个 listener 以 `background=False` 复用同一
lifespan。`deps.get_agent_job_store()` 返回 `AgentJobStore | None`，未配置时保持既有 503 行为。

### 2.4 真实数据库测试

`tests/` 下没有 `conftest.py`；基线 `59bf2cc` 上有 **11 个** dbtest 文件 import store，各自建
pool，其中 `test_agent_job_store.py` 引用 67 个 store 方法。快速层替身包括
`tests/servers/test_agent_jobs_api.py` 的 `FakeAgentJobStore` 与 `tests/unit/test_capacity_history.py`
的内联替身。

§4.1 的搬迁把这一个数字降到了 10：`test_admin_observability.py` 需要的只是 `load_json`，因此不再
依赖 store。这类副产物是搬迁的直接收益，不是附带损伤。

pytest 默认 `addopts` 含 `--maxfail=10`；全量诊断必须 `-o addopts=`。

---

## 3. 目标结构

```text
API / 后台业务函数 ──→ ports / domain
                          ↑ 实现
                   storage/store.py（阶段 1 仍然实现全部 port）
                          └──→ storage/_sql ──→ connection

lifespan ──→ 构造 store ──→ deps 从中提供窄 port
```

| 位置 | 职责 |
|---|---|
| `cloud_agent/domain/` | 状态常量、ID 生成、纯校验与业务值 |
| `cloud_agent/ports/` | 每能力一个 Protocol；参数/返回值/optional 性与基线一致 |
| `cloud_agent/storage/_sql/` | 行投影、SQL 常量、connection 级 helper |
| `cloud_agent/storage/store.py` | 阶段 1 的实现；阶段 2 逐能力清空，阶段 3 删除 |
| `cloud_agent/api/deps.py` | 返回所需能力；未配置保持 503 |

**关键事实：Python 的 Protocol 是结构化的。** 只要 `AgentJobStore` 的方法签名与 Protocol 一致，
它就能直接赋给 `Protocol` 类型的参数，**不需要 facade、不需要改 SQL、不需要去掉 `self`**。
这一点已在真实代码上验证：15 个 `takes_*(store)` 调用在 mypy 下零错误，而错接对象的负例如期报错。

**阶段 1 保持现有 store 实现全部 port**，保留启动构造入口，不提前拆 repositories、不删除 store。

---

## 4. 阶段 1：窄 Protocol + 共享名字归位 + 调用方收窄（SQL 一行不动）

### 4.1 交付物

1. `cloud_agent/ports/` 下 **16 个 Protocol、114 个方法**，签名与基线逐字一致（含 keyword-only、
   默认值、返回类型、`None` 语义）。方法清单由代码承载，本文不重复。
2. **共享常量与纯函数归位**（移动，不是复制）：`store.py` 反向 import，因此 SQL 不变，但从第一天
   起每个名字只有一份定义。

   | 名字 | 新归属 |
   |---|---|
   | `QUEUED`/`RUNNING`/`SUCCEEDED`/`FAILED`/`CANCELLED`/`TERMINAL_STATES` | `cloud_agent/domain/execution.py` |
   | `ATTEMPT_*`、`EVENT_ATTEMPT_*` 常量 | `cloud_agent/domain/execution.py` |
   | `request_fingerprint`、`_check_client_request` | `cloud_agent/domain/requests.py` |
   | `_load_json` | `cloud_agent/storage/_sql/rows.py` |
   | `NEXT_ATTEMPT_START_SQL` | `cloud_agent/storage/_sql/attempts.py` |
   | `LEDGER_WINDOW_UNTIL_SQL` | `cloud_agent/storage/_sql/ledger.py` |
   | `CAPACITY_HISTORY_MAX_AGE_S`、`CAPACITY_HISTORY_RETENTION_S` | `cloud_agent/capacity_protocol.py` |

   容量常量归入 `capacity_protocol.py` 而不是新建 `domain/capacity.py`：该模块已经是容量领域的
   归属地（持有 `HISTORY_RESOLUTION_SECONDS`、`MAX_HISTORY_POINTS` 等），且已被 store 与两个调用方
   导入。`LEDGER_WINDOW_UNTIL_SQL` 是对 `NEXT_ATTEMPT_START_SQL` 的 f-string，故 `_sql/ledger.py`
   从 `_sql/attempts.py` 导入。

   **容量常量必须离开 `store.py`**：`prune_capacity_history` 的默认值是
   `CAPACITY_HISTORY_RETENTION_S`，而 port 不得 import `cloud_agent.storage`。

3. 调用方改为依赖自己使用的窄 port。
4. 严格类型检查门禁，配置见 §4.2，覆盖完整 `cloud_agent/`，**包含 store，不排除**。
5. 边界测试：
   - **结构一致性**：逐个 port 断言真实 `AgentJobStore` 具有全部成员且可调用，失败即报缺失成员名；
   - **边界扫描**：从调用方集合出发证明覆盖（支持导入别名、依赖 resolver 返回值、
     `_require_store(x).method(...)` 嵌套调用），不支持的动态情况列为待处理，不得静默跳过；
   - **错接 port 负例**：一个刻意错接对象的用例，必须失败。门禁「能拦住」需要证据，不是断言。

### 4.2 类型检查门禁

配置固定在 `pyproject.toml` 的 `[tool.mypy]`：

```toml
python_version = "3.12"
files = ["cloud_agent"]
follow_imports = "silent"
ignore_missing_imports = true
check_untyped_defs = true
disallow_untyped_defs = true
disallow_incomplete_defs = true
disallow_any_generics = true
warn_unused_ignores = true
show_error_codes = true
```

`59bf2cc` 上的实测基线：**66 errors / 14 files（checked 67 source files）**。分布：
`schemas.py` 15、`api/hosts.py` 14、`api/routes.py` 9、`app.py` 6、`capacity_protocol.py` 5、
`source_control.py` 3、`session_statistics.py` 3、`grant_usage.py` 3、`storage/store.py` 2、
`api/admin_hosts.py` 2、`deploy_readiness.py`/`api/session.py`/`api/deps.py`/`admin_observability.py` 各 1。

**66 是待清零的读数，不是验收目标。** 阶段 1 的验收是**该配置下零错误**，并接入 blocking CI
（`make typecheck` + `ci.yml` step，`continue-on-error` 不设）。

不得通过 `Any`、`cast`、`type: ignore` 或缩小检查范围来掩盖错接 port。清理过期 `type: ignore`
是允许的，因为它让检查更严而不是更松。

### 4.3 退出条件

`make lint`、单元/服务层、真库层全绿；调用方 mypy 零错误；结构一致性、边界扫描与错接 port 负例
全部通过；`store.py` 的 SQL 与绑定参数在 `git diff` 下无变化（类型修正除外，且逐条说明）。

---

## 5. 阶段 2：按能力迁出实现

| 组 | 能力 | 方法数 | 为什么这个顺序 |
|---|---|---|---|
| G1 | `HostPoolRepository`、`HostCapacityRepository`、`GrantRepository`、`LedgerRepository`、`McpConnectionRepository`、`SourceControlRepository` | 43 | 每个都有独立的真库测试文件，迁完立刻能用真实 repository 验证；`#155` 有同形状参考 |
| G2 | `ConversationQueries`、`ExecutionQueries`、`CredentialQueries`、`AuditRepository` | 14 | 以读为主，行为可用真库断言逐条对照 |
| G3 | `ConversationCommands`、`AttemptCommands` | 19 | 写路径与事务所有权，幂等与生命周期语义集中在这里 |
| G4 | `SandboxRepository`、`ResidencyRepository`、`SessionForkRepository`、`SnapshotRepository` | 38 | 最复杂；R1/R4/R5/R7 等历史回归都在这一组 |

每个能力的完成定义（六条，缺一不可）：

1. repository 实现落在 `storage/repositories/<module>.py`，**保留事务所有权与锁顺序**；可以直接
   持有同一个 pool。
2. 同一时刻该能力**只有一个实现**：迁出后 `store.py` 的同名方法改为纯委托，或删除。
3. 该能力的调用方持有的类型从 `AgentJobStore` 换成对应 Protocol。
4. 该能力自己的真库测试跑在**真实 repository** 上，不再经过 monolith。
5. 该能力涉及的历史回归（附录 B 的 R1–R8）在真实实现上可证伪。
6. `make lint` + 单元/服务层 + 真库层全绿；提交里附该能力的一致性报告与测试 node ID。

**不得跳过第 4 条**：一个能力的 dbtest 没有跑在真实实现上，就不算迁完。

---

## 6. 阶段 3：删除委托与旧 store

1. 删除 `store.py`、`get_agent_job_store`、三处 `_require_store`、两处 `._pool` 穿透；状态常量与
   loader 从新归属导入，不残留「只 import 一个常量」的生产依赖。
2. 不以 re-export、兼容 facade 或改名文件复制 monolith。
3. 架构测试扫描生产与测试 Python 路径：没有旧入口、没有越界 pool 访问、没有未登记的生产具体
   实现依赖。
4. 把「启动不执行 DDL/回填」的基线测试迁移为对完整存储层与装配路径的同等或更强约束。

---

## 7. 生成器与验证器：工具，不是项目

生成器（`tools/split_store.py` 等，草稿在 `bf952d2`）只用于**批量搬运方法体**。设计**不得依赖
它**：任何一组能力都可以手工迁移，只要满足 §5 的六条。

必须修的只有两件事：一致性检查器的两处不安全归一化（`merge_adjacent_literals` 与字符串内部空白
压缩），以及从**锁定的 Git 对象**读取基线 `store.py` 而不是工作区副本。

`_GROUPS`/`GLOBAL_MOVES`/`HELPER_MOVES` 的漂移、`--out` 临时目录流程**可以不做**：它们只在
「从基线一次性重新生成整棵树」时才必须，而分阶段路线不采用那条路线。**生成器不得成为阶段的
前置条件。**

`cloud_agent/ports/` 的方法签名与 docstring 由一次性 AST 抽取生成后作为普通源码提交，抽取脚本
不保留——「port 不能描述 store 没有的方法」由结构一致性测试持续保证，而不是由脚本保证。

---

## 8. 真库测试迁移（随能力推进）

- fixture 自建 pool，直接构造该能力的 repository；测试显式取得自己需要的 port。
- **按组迁移**：迁 G1 时改 G1 的测试文件，依此类推；`test_agent_job_store.py` 随 G3/G4 逐步拆解。
- API 集成测试使用生产装配函数（允许替换外部 HTTP 服务），不得另造不完整装配。
- 在独立 Postgres 测试库应用最新迁移（head `0006`）；库名必须含 `_test_`。

---

## 9. 验收证据与环境

### 9.1 证据

每个阶段、每个能力组都要有：`source_base_sha`、`candidate_sha`（实际被测提交）、
`worktree_clean_before/after`、`command`、`exit_code`、`raw_log`、`collection_file`（pytest node ID
清单）、`environment`。禁止引用 stash、旧提交、未结束命令或旧收集集的数字。

全量诊断：

```bash
uv run pytest -q -o addopts= -m 'not external and not dbtest'
uv run pytest -q -o addopts= -m dbtest
```

### 9.2 环境

- `requires-python >=3.12,<3.14`；本机 Python 3.12.13，mypy 2.3.0。
- 本机 `postgresql@15` 运行中，`127.0.0.1:5432`，15.17，UTF8 / `en_US.UTF-8`；使用 ENVIRONMENT.md
  的五个 `TEST_DB_*`，新建专用 `_test_` 库，不复用任何既有测试库。
- 工作树在持久路径 `wt/cloud-agent-storage-rebuild`，每阶段结束用 `git bundle create` 在
  `wt/snapshots/` 做一份本地快照。本次不推送远端。
- mypy 需要可写的 cache 目录，ruff 需要 `--no-cache`（在拒绝工作区外写入的沙箱中）。

### 9.3 Docker 与 stack smoke（本阶段明确不做）

本次不启动 Docker（机器上无 Docker Desktop，colima 两个 profile 均 Stopped）。完整本地 stack
smoke 不执行；交付结论必须写成「实现阶段已完成，stack 验收待做」，并列出因此未跑的具体门槛。
不得自行 `colima start` 或复用来源不明的旧镜像。

---

## 10. 漂移处理

基线锁定后，任何改动 `store.py`、`migrations/` 或 admin read model 的提交都是漂移：外围 API/admin
变化 → 移植调用方并重测；SQL 或 helper 变化 → 更新受影响能力的迁移与对照基线（**不重跑整棵树**）；
`MERGEABLE` 不代表无语义漂移。

本次实施期已发生一次真实漂移：基线由 `cba9a47`/`f9a9f5f` 推进到 `59bf2cc`，`store.py` 增加 4 个
方法、2 处签名变化、1 个 migration。处理方式是**按实际 SHA 重新核对方法归属、Protocol 签名与
全部调用方**，而不是机械沿用旧的方法数与分组。

---

## 附录 A：能力分组与方法归属

方法清单由 `cloud_agent/ports/` 的代码承载（16 个 Protocol、114 个方法），本文不复制，以免与代码
漂移。分组见 §5。`initialize` 不属于任何 port，随阶段 3 的启动路径一起处理。

## 附录 B：必须成为回归约束的历史调用错误

R1 提交后读回的 callback 参数（finalize 等，成功与幂等重试都不抛 `TypeError`）；R2 follow-up 先读
parent job（读用 `ConversationQueries`、写用 `ConversationCommands`、授权用
`SourceControlRepository`）；R3 worker claim 的仓库授权（有合法 repo grant 的连接用户不被误拒
403）；R4 residency grant 用 `GrantRepository` 记录与关联，失败时撤销与清理正确；R5 terminal
resume 写事件用 `AttemptCommands`，恢复后不返回 500 也不重新 suspend；R6 MCP 列表/删除与 GitLab
disconnect 只使用各自 port；R7 fork 失败清理的闭包引用不被同名局部字典覆盖；R8 SSE 通过 queries
读 job、execution queries 读事件，多页排空后收到 `job_finished`，`Last-Event-ID` 续传正确。

另需覆盖 heartbeat、release-claim、MCP registry/resolve 的未配置依赖路径；至少保留一条错接 port
负例与一条 Boundary 错映射负例。

---

## 11. 实施结果（2026-09-11）

三个阶段全部落地。`cloud_agent/storage/store.py` 已删除。

### 11.1 与设计的差异

| 设计 | 实际做法 | 为什么 |
|---|---|---|
| §5 G3 的共享私有 helper，建议顺序 (c) → (a) | 直接做 (a)：`_insert_event` 与两个 `_fail_*_follow_ups` 迁入 `storage/_sql`，`conn` 作首参 | 实测三者都不碰 `self`，(a) 是搬移而非改造，不需要 (c) 的调查作前置 |
| — | `_release_host_slot` 也迁入 `_sql/hosts.py` | 它写 `agent_runner_hosts`，调用方全是 attempt——与 `insert_event` 同形，不应让 AttemptCommands 拥有 host 表的语句 |
| — | 十六个实现统一加 `Postgres` 前缀 | port 与实现原本同名（16 个里 10 个）；阶段 3 的装配根需要同时提到两者 |
| §6.1「删除三处 `_require_store`」 | 保留各模块自己的 `_require_store`，但没有一个再以具体类为类型 | 它只做去 `None`，本身不是越界；越界的是"以 monolith 为类型" |
| §6.1「两处 `._pool` 穿透」 | 两个 admin 读模型进入装配（`storage.admin_runs` / `storage.admin_usage`） | 穿透的成因是它们无处可取；给它们一个归属比禁止取用更根本。二者仍是具体类，装配处注明「尚无 port」 |

### 11.2 关于「方法体零改动」

搬运用逐行拷贝，不用 `textwrap.dedent`：dedent 会连三引号 SQL 字面量内部的缩进一起左移，
Postgres 收到的语句就不再是原来的语句（§7 已警告过这一点）。校验方式是重新解析两侧比对：
方法体、参数、返回标注、async 性逐字相同，docstring 按 `clean=True` 比（它确实要跟着函数走）。
共校验 114 个公开方法 + 12 个私有 helper，零差异。

### 11.3 边界的持续约束

| 测试 | 约束 |
|---|---|
| `tests/unit/test_storage_ports.py` | port 与 repository 签名/docstring 逐字一致；每个公开方法恰属一个 port；port 不 import storage；从调用点反扫覆盖 |
| `tests/unit/test_storage_ports_gate.py` | mypy 必须拒绝刻意错接的 fixture（含「十个方法齐全、一个关键字拼写不同」——`runtime_checkable` 会放行的那种） |
| `tests/unit/test_storage_boundary.py` | 旧入口在生产与测试中都不存在；`storage/` 之外无越界 pool 访问；`Postgres*` 只在装配与 repositories 中出现；装配的属性与 port 一一对应 |
| `tests/unit/test_schema_baseline.py` | 原先只扫 `store.py`，现扫整个 `cloud_agent/storage/`——同样的断言，覆盖面更大 |

### 11.4 验收

`ruff format` / `ruff check` / `mypy`（99 文件，0 错误）/ `pydocstyle` 全绿；
`pytest -m "not external and not dbtest"` **2756 passed**（原 2750 + 6 个新边界测试）；
`TEST_DB_NAME=cloud_agent_storage_test_db pytest -m dbtest` **371 passed**。
两个数字与拆分前一致。Docker 与 stack smoke 仍未执行（§9.3 的限制不变）。
