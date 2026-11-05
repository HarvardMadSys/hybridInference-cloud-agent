# 管理员审计日志重新设计

日期：2026-11-05。状态：第 1–3 期**已实现**（`murphy/audit/redesign`），只追加触发器和第 4 期
（服务身份）待做。第 9 节的四项决定都已于 2026-11-05 确认。实现和本文前面几节不一致的地方，
以第 14 节"实现记录"为准。

本文取代 `agent_admin_audit_log` 现有的写入约定（`api/deps.py:556` 的
`log_admin_action`、`storage/_sql/local_accounts.py:134` 的 `write_audit`）和
Audit log 页面的读取约定（`api/admin_audit.py`）。参考了 FreeInference Gateway
在 2026-10-25 做的审计归属设计（`HarvardMadSys/hybridInference`
`docs/agents/specs/2026-10-25-audit-attribution-design.md`），沿用它的主体类型和对象类型，
并补上它没有解决的部分。

## 1. 现状

### 1.1 数据（staging `cloudagent_accounts`，截至 2026-11-05）

| 来源 | 行数 | 写入者 |
|---|---|---|
| Servers 页面轮询读取（`agent.compute.*.list`），10-09 起已不再写 | 11,582 | 真人 admin |
| `runner_host.drain` / `undrain` / `set_active` | 约 370 | 大部分是 `admin-token`，即 freeInference 部署流水线的 `ops/agent_deployment_drain.py` |
| `sandbox.delete.discarded` / `failed` / `completed` / `archived` | 171 | `sandbox-deletion-worker` |
| `sandbox.delete.requested` | 102 | 真人 admin（10-19 一次批量清理就有 90 条） |
| `local_account.approve` / `grant_admin` | 5 | 3 条来自 `deployment-bootstrap` |
| `local_account.signup` | 1 | `anonymous` |
| 其余真人 admin 的变更 | 约 150 | 真人 admin |

真人 admin 每天的真实操作通常只有 0 到 3 条，却被自动化事件和 worker 事件淹没了。

### 1.2 缺陷

1. **actor 只是一个字符串。** `admin:<id>`、`admin-token`、`deployment-bootstrap`、
   `sandbox-deletion-worker`、`anonymous`、`admin:staging-cleanup` 全部写在同一个
   `actor_id` 字段里。页面分不出哪些是人、哪些是自动化、哪些是用户自己的操作。
2. **target 只能是用户。** `target_user_id` 之外的对象（主机、沙箱、服务器、设置）只能塞进
   `details`。所以"这台主机被谁动过"和"这个用户的沙箱被谁删过"都查不了。
3. **"谁对哪个用户做了什么"读不出来。** 页面没有账号分类；`local_account.*` 没有可读的描述；
   actor 只显示 ULID 的前 8 位；被操作的用户只在展开后显示 ULID；也不能按用户筛选。
4. **不同性质的事件混在一起显示**：admin 变更、用户自助操作（注册、改密码、绑手机）、
   worker 的生命周期事件、部署自动化。
5. **审计写失败时没有任何可见的信号。** API 层的写入（`log_admin_action`）在变更完成之后才补写，
   写失败只记一条 warning（`agent_admin_audit_write_failed`），既没有计数，也没有地方能看到。
6. **噪音靠读取时的排除名单过滤**（`POLLING_READS`），分类靠手工维护前缀表
   （`AUDIT_CATEGORIES`），描述又在前端另写一份（`describe.ts`）。每新增一个 action 要改三处，
   漏改一处就会显示成兜底文字或者归错类。
7. **共享的 `ADMIN_TOKEN` 不可归属。** 部署流水线、主机上的脚本和运维人员手工操作都用同一个
   token，事后无法区分是谁做的。
8. **删除账号时会把审计历史一起删掉**（`storage/_sql/local_erasure.py:304`）。
   这个人当初是谁批准的、谁给的 admin，删除后都查不到了。
9. **`success` 布尔值语义含糊。** compute 的大部分操作在请求入队时就记成成功（返回 202），
   实际执行可能失败。`describe.ts` 只能靠把句子写成 "Requested ..." 来弥补；
   测量开关则把真实结果另外放在 `details.outcome` 里。

### 1.3 已经做对的部分，保留

- 读接口和写接口分开（`ports/audit.py` 与 `ports/audit_log.py`）。
- keyset 分页（`before_id`）。
- 每个 action 有一句可读的描述，并区分 `replayed` 和 `refused`。
- admin 看用户数据的页面（Runs、Conversations、Sandboxes 的 trajectory）本身就不含内容
  （`admin_observability.py:1`）。所以能直接看到用户数据的，只剩 pgAdmin 和数据库查询这两个入口。
- 团队审计（`agent_team_audit_events`）独立成表，给团队 owner 看。这个设计保持不变。

## 2. 目标与非目标

**目标**

- 审计日志只回答一个问题：**谁，以什么身份，对谁或什么做了什么，结果如何，为什么做。**
- 默认视图只显示真人 admin 的操作，噪音从写入端就不产生。
- 能按用户、按对象、按操作者查完整历史。
- **审计记录永远不删除、不修改**，包括删除账号之后。
- 审计写失败**不影响**操作本身，但失败必须能被看到。
- 新增一个 action 只改一处，漏注册会让测试失败。

**非目标**

- 防御数据库超级用户篡改（哈希链、外部 WORM 存储）。第 6.4 节的触发器只防代码 bug。
- 记录 pgAdmin 会话里执行的每条 SQL。只记录"谁打开了 pgAdmin"，见第 10 节。
- 在用户自己的 Settings 页面展示账号活动。
- 接入 SIEM、导出、告警推送。
- 合并团队审计表。

## 3. 什么事件进审计表

一个事件进审计表，当且仅当它满足下面三条之一：

1. **特权变更（`kind = change`）**：凭 admin 或服务身份，改变了平台状态或他人的账号、数据。
2. **特权访问（`kind = access`）**：凭 admin 身份，取得了直接访问他人数据的能力，
   比如打开 pgAdmin、执行数据库查询。
3. **账号自助变更（`kind = self_service`）**：用户对自己账号的关键变更，包括注册、改密码、
   绑邮箱和手机、自助删除。这类记录只给 admin 看，**默认隐藏**；在用户详情页的历史里显示。

以下事件**不进**审计表：

| 事件 | 去处 |
|---|---|
| 只读查询、轮询 | 不记录 |
| worker 执行 admin 请求的中间步骤（重试、恢复） | 结构化日志和对象自身的状态表 |
| worker 执行 admin 请求的**最终结果** | 写入审计结果表，挂在原记录下（第 5.2 节） |
| 登录成功或失败 | `agent_local_login_events`，不变 |
| 团队内部操作 | `agent_team_audit_events`，不变 |

`system` 主体只在一种情况下写入审计表：平台**按策略主动**改变他人状态，
而且没有对应的 admin 请求。例如按保留期策略自动删除沙箱。这时 `details.policy`
记下依据的是哪个设置。目前还没有这类写入者，先把类型留出来。

## 4. 主体与对象

### 4.1 主体（actor）

| `actor_type` | 含义 | `actor_id` | 默认显示 |
|---|---|---|---|
| `admin` | 用 session 登录的管理员账号 | 账号 id | 是 |
| `break_glass` | 不属于任何账号的带外运维入口 | `admin-token` 或 `bootstrap-cli` | 是，并且高亮 |
| `service` | 有名字的服务身份（第 8 节） | 服务名，如 `deploy-pipeline` | 否 |
| `system` | 平台按策略主动执行 | 策略或 worker 名 | 否 |
| `user` | 用户对自己账号的操作 | 账号 id | 否 |
| `anonymous` | 未登录的请求（注册） | `anonymous` | 否 |

和 FreeInference 的区别：新增了 `service`，并把 `admin_token` 和 bootstrap 合并为
`break_glass`。break glass 的每次使用**始终显示，并且高亮**，不受"隐藏自动化"开关影响，
因为它本身就是需要人留意的信号。

### 4.2 对象和受影响用户

- `target_type` / `target_id`：被操作的对象。类型包括 `account`、`host`、`sandbox`、
  `compute_server`、`compute_connection`、`compute_operation`、`setting`、
  `signup_domain`、`broadcast`、`database`、`measurement`、`runtime`。
- **`subject_user_id`：受影响的用户。** 它和 target 分开存。删除一个沙箱时，
  target 是 `sandbox:asbx_…`，subject 是这个沙箱的 owner。这样按用户查历史时，
  直接对账号做的操作和对他名下资源做的操作都能查到。
  FreeInference 把 API key 的 target 强行记成 key 的 owner，就是因为缺少这个字段。

### 4.3 名字快照

`actor_label`、`target_label`、`subject_label` 在**写入时**存下当时的登录名或邮箱、主机名等。
这样读取时不需要 join，显示的也是事件发生当时的名字，之后改名或删除账号都不影响历史记录。

删除账号会释放登录名和邮箱，新账号可以重新使用（`accounts.py` 的 erase）。所以单看名字，
可能分不清是哪一个账号。页面在显示名字时必须同时带上账号 id 的短格式；
如果这个账号已经被删除，再加一个"已删除"标记。

## 5. 表结构

在原表上修改，不新建表。迁移步骤见第 11 节。

### 5.1 `agent_admin_audit_log`

```sql
id               BIGSERIAL PRIMARY KEY,
created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
action           TEXT NOT NULL,
category         TEXT NOT NULL,                 -- 写入时由注册表决定
kind             TEXT NOT NULL CHECK (kind IN ('change', 'access', 'self_service')),
actor_type       TEXT NOT NULL CHECK (actor_type IN
                   ('admin', 'break_glass', 'service', 'system', 'user', 'anonymous')),
actor_id         TEXT NOT NULL,
actor_label      TEXT,
source_ip        TEXT,                          -- 可信的客户端地址；没有时为 NULL，不写 'unknown'
request_id       UUID,                          -- 同一次 HTTP 请求产生的多条记录，以及对应的服务端日志
target_type      TEXT NOT NULL,
target_id        TEXT NOT NULL,
target_label     TEXT,
subject_user_id  TEXT,                          -- 受影响的用户；不设外键
subject_label    TEXT,
outcome          TEXT NOT NULL CHECK (outcome IN ('applied', 'requested', 'refused', 'failed', 'replayed')),
reason           TEXT CHECK (reason IS NULL OR length(reason) <= 500),
details          JSONB NOT NULL DEFAULT '{}'::jsonb
```

索引：`(id DESC)`、`(subject_user_id, id DESC)`、`(target_type, target_id, id DESC)`、
`(actor_type, actor_id, id DESC)`、`(category, id DESC)`。

- `outcome` 取代 `success`：
  - `applied`：已生效。
  - `requested`：已受理，实际执行异步进行，比如 compute 操作和沙箱删除。
  - `refused`：被业务规则拒绝，没有改变任何状态。
  - `failed`：执行出错。
  - `replayed`：重复请求，没有改变任何状态。
- `actor_id` 和 `subject_user_id` 不设外键，原因与 FreeInference 相同：
  删除账号不应该级联删掉这些记录，也不应该被它们阻止。
- `reason`：管理员在批准、停用、删除等操作时填写的理由。现在只记了 `reason_given` 布尔值，
  理由本身丢掉了。

### 5.2 `agent_admin_audit_outcomes`

```sql
event_id     BIGINT PRIMARY KEY REFERENCES agent_admin_audit_log (id),
outcome      TEXT NOT NULL CHECK (outcome IN ('applied', 'failed', 'cancelled')),
created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
reported_by  TEXT NOT NULL,                     -- 'sandbox-deletion-worker'、'compute-operations'
details      JSONB NOT NULL DEFAULT '{}'::jsonb
```

一条 `requested` 记录最多对应**一个最终结果**。中间的重试和恢复不写这里。原记录本身不修改，
所以审计表依然只追加。页面上显示成：**"Requested deleting sandbox asbx_… → Completed 2 min later"**。

## 6. 写入

### 6.1 操作注册表：唯一来源

新增 `cloud_agent/audit/registry.py`：

```python
@dataclass(frozen=True)
class AuditAction:
    name: str                       # 'agent.runner_host.drain'
    category: AuditCategory         # 'servers'
    kind: Literal['change', 'access', 'self_service']
    target_type: TargetType         # 'host'
    details: type[BaseModel]        # 允许记录的字段；凭据字段不可能出现
    asynchronous: bool = False      # True 表示写 'requested'，最终结果另外上报
```

- 分类（`category`）只有：`accounts`、`settings`、`servers`、`sandboxes`、
  `sensitive_access`、`measurements`、`access_control`（signup 域名、服务身份）。
- 注册表通过 `GET /admin/agent/audit-log/actions` 导出。前端测试检查注册表里的每个 action
  在 `describe.ts` 里都有描述句子，所以不会再出现 "Local account approve" 这种兜底文字。
- 后端测试扫描所有 `audit.record(...)` 调用，确认 action 都已注册。
- 运行时遇到未注册的 action，或 `details` 校验不通过时，**不抛异常**：照样写入，
  `category` 记为 `uncategorized`，`details` 原样保存，同时记一条 error 日志。
  原则是宁可分类不准，也不能丢记录。

### 6.2 写入接口和失败处理

```python
await audit.record(
    conn,                                   # 有事务时传入同一个连接
    AuditActions.RUNNER_HOST_DRAIN,
    actor=ctx.actor,                        # 来自依赖项 get_audit_actor
    target=Target.host(name),
    subject=None,
    details=DrainDetails(reason=...),
    outcome="applied",
    reason=payload.reason,
)
```

**审计写失败绝不让操作失败。**

- 如果变更本身是一次数据库写入：审计记录写在**同一个事务里，但包在一个 SAVEPOINT 中**。
  审计插入失败时只回滚到这个 savepoint，变更照常提交。这样正常情况下，变更和记录一定同时存在；
  审计插入出问题时，也不会拖垮变更。现在账号相关的写入（`write_audit`）在事务里直接插入，
  插入失败会让整个变更回滚，这个行为要改。
- 如果变更发生在外部（provider API、主机命令）：先写 `requested` 记录，再派发执行；
  执行完由 worker 写入第 5.2 节的结果表。任何一步审计写失败，都不阻止派发和执行。
- 访问类事件（打开 pgAdmin、数据库查询）也一样：记录写不进去，照样放行。
- **写失败必须能被看到：** 记一条 error 日志（`agent_admin_audit_write_failed`，带上 action、
  actor 和 target），并累加一个计数器。Audit log 页面顶部显示"最近 7 天有 N 条审计写入失败"，
  点开可以看到失败的 action 和时间。这个计数存在一张小表里，不依赖日志系统。
- `verify_admin_access` 不再返回 `"admin:<id>"` 或 `"admin-token"` 这样的字符串，
  改为返回 `AuditActor`，其中已经带上 `actor_label` 和 `source_ip`。
- 新增请求 id 中间件：读取或生成 `X-Request-ID`，同时写进审计记录和结构化日志。

### 6.3 删除账号：审计记录完整保留

删除账号时**不删除、不修改**任何审计记录，名字快照也保留。

- 删掉 `local_erasure.py:304` 的 `DELETE FROM agent_admin_audit_log WHERE target_user_id = $1`。
- 修改 `accounts.py` 中 erase 的 docstring。现在它写的是 "… and earlier audit rows"，
  删除后要写清楚：审计记录保留。
- 自助删除账号（`self_delete`）同样处理。
- erase 本身写一条记录，`details` 里带上删除的数据行数，这是现有行为，保持不变。

### 6.4 只追加

加一个触发器：任何对 `agent_admin_audit_log` 或 `agent_admin_audit_outcomes` 的
`UPDATE` 或 `DELETE` 都直接报错，没有例外。这个触发器防的是代码 bug，防不了数据库 owner。
第 11 节一次性迁移的清理操作，在建触发器之前执行。

## 7. （已删除）

原先设计的单独账号活动表已取消。用户自助操作仍写在审计表里，见第 3 节第 3 条。

## 8. 服务身份，取代共享的 `ADMIN_TOKEN`（已确认，第 4 期）

新表 `agent_service_principals`：

| 字段 | 含义 |
|---|---|
| `name` | 主键，例如 `deploy-pipeline` |
| `token_hash` | token 的哈希，明文只在创建时显示一次 |
| `scopes` | `TEXT[]`，例如 `{hosts.drain, hosts.activate}` |
| `created_by`、`created_at`、`last_used_at`、`revoked_at` | 创建者、创建时间、最近使用、吊销时间 |

- admin 在后台创建和吊销服务身份。这两个操作本身记入审计，分类为 `access_control`。
- 每个服务身份只能访问它 scope 覆盖的路由，其他路由一律返回 403。
- freeInference 的部署流水线改用 `deploy-pipeline` 身份。这需要先列出
  `ops/agent_deployment_drain.py` 调用的所有路由，再在 freeInference 那边改 secret。
- `ADMIN_TOKEN` 保留，作为 break glass 入口。

## 9. 决定

| # | 决定 | 状态 |
|---|---|---|
| 1 | 删除账号时，审计记录完整保留，名字也不抹掉 | 2026-11-05 确认 |
| 2 | 审计写失败不让操作失败；失败要能被看到 | 2026-11-05 确认 |
| 3 | 部署流水线改用服务身份（第 8 节） | 2026-11-05 确认，尚未实现 |
| 4 | 不给用户展示自己的账号活动 | 2026-11-05 确认 |

## 10. 页面

**列表**
- 默认只显示 `admin` 和 `break_glass` 主体的记录。有一个开关
  "包含自动化和用户自助操作"，打开后显示另外四种主体。
- 筛选条件：分类、操作者、受影响用户、对象（类型 + id）、kind。
- 每一行包含：操作者名字（break glass 用红色标记）、一句描述（带对象名和受影响用户链接）、
  outcome（`requested` 的记录同时显示最终结果和耗时）、相对时间。
- 展开后显示：action、请求 id、来源 IP、理由、details。
- 页面顶部：最近 7 天审计写入失败的条数（第 6.2 节），为 0 时不显示。

**入口**
- 用户详情页：一个"历史"标签页，显示 `subject_user_id` 等于该用户的所有记录，
  admin 的操作和用户自助操作都显示。
- 主机、沙箱、服务器详情页：一个 "History" 链接，带上对象筛选条件跳转到审计页。

**pgAdmin**
- 只记录 "Opened pgAdmin"。如果 pgAdmin 使用独立的数据库角色，可以另外给这个角色打开
  Postgres 的 `log_statement`，但不在本设计范围内。

## 11. 迁移

旧记录的 actor 字符串格式都是固定的，所以**可以确定性地回填，不需要猜**。
FreeInference 当初不回填，是因为它的 `admin_ip` 一个字段里混着 IP、邮箱和登录名。

| 旧记录 | 处理 |
|---|---|
| `agent.compute.*.list` 等 6 种轮询读取（11,582 行） | 删除 |
| `actor_id = 'admin:<id>'` | `admin` 主体，从账号表回填 label |
| `actor_id = 'admin:staging-cleanup'` | `service` 主体，名字为 `staging-cleanup` |
| `actor_id = 'admin-token'` | `break_glass` / `admin-token`。不根据 IP 猜测是不是部署流水线 |
| `actor_id = 'deployment-bootstrap'` | `break_glass` / `bootstrap-cli` |
| `sandbox-deletion-worker`（171 行） | 按 `details.request_id` 挂到对应的 `delete.requested` 上，转成结果表的行，然后删除原行。staging 上 171/171 都能对上；对不上的保留为 `system` 主体 |
| `local_account.signup` | `anonymous` 主体，`kind = self_service` |
| `local_account.password_set` / `bind_phone` / `request_bind_email` / `self_delete` | `user` 主体，`kind = self_service` |
| `agent.compute.connection.validate` | 删除：只读检查，不改变状态 |
| target 和 subject | 按注册表从 action 和 `details`（`host`、`sandbox_id` 等）推导；沙箱的 subject 从沙箱表回填 owner |
| `success` | `true` 映射为 `applied`，`false` 映射为 `failed`；`details.outcome` 为 `refused` 或 `replayed` 时以它为准；compute 的入队类操作映射为 `requested` |

这次迁移是**唯一一次**删除审计行，删的只有上面三类：轮询读取、`connection.validate`，
以及已经转进结果表的 worker 行。迁移完成后才建第 6.4 节的触发器。

## 12. 分期

| 期 | 内容 | PR |
|---|---|---|
| 1 | 注册表和 `audit.record`；表结构改动和回填；`get_audit_actor`；所有写入者改为 savepoint 写入；结果表；写失败计数；删除 `log_admin_action` | 后端 1 个，迁移 1 个 |
| 2 | 新的列表 API、页面筛选、默认隐藏非 admin 主体、用户详情页"历史"标签、对象 History 入口 | 前端 1 个 |
| 3 | 删除账号时保留审计记录；只追加触发器 | 1 个 |
| 4 | 服务身份，以及 freeInference 部署流水线切换（待确认） | 本仓库 1 个，freeInference 1 个 |

第 1、2 期做完，最大的两个问题就解决了：噪音默认不出现，"谁对谁做了什么"可以读出来。
第 3 期很小，也可以并进第 1 期。

## 13. 测试

- 注册表完整性：每个 `audit.record` 调用的 action 都已注册；前端每个注册的 action 都有描述句子。
- 写失败不影响操作：故意让审计插入失败时，变更依然提交，接口返回成功，失败计数加 1。
- 噪音回归：每个 admin `GET` 路由都不产生审计记录。
- 删除账号：删除后，所有和该账号有关的审计记录都还在，名字快照不变。
- 只追加：直接执行 `UPDATE` 或 `DELETE` 时，触发器报错。
- 回填：用一份 staging 数据快照跑迁移，核对行数、映射结果和结果表的对应关系。

## 14. 实现记录（2026-11-05）

实现时有以下几处与前面的设计不同，以这里为准。

1. **`category` 和 `kind` 不入库，读取时由注册表推导。** 筛选在 SQL 里用注册表给出的 action
   列表做（`actions_in(category)`）。这样以后调整分类不需要改写历史数据。
2. **`actor_type` 是数据库的生成列**，由 `actor_id` 推导（迁移 `0114_admin_audit_attribution`）。
   `actor_id` 沿用原来的格式（`admin:<id>`、`self:<id>`、`admin-token`、worker 名）。这样滚动发布
   期间上一个版本写入的行也能被正确归类，旧版本的读取和写入都不受影响。
   历史上手工写入的 `operator:*` 归为 `break_glass`；`admin:staging-cleanup` 和
   `codex-ops-stale-cleanup` 归为 `service`。
3. **用 `correlation_id` 代替 HTTP `request_id`。** 它存的是异步工作的标识：沙箱删除的
   request id，或 compute 操作的 operation id。这次没有加请求 id 中间件。
4. **结果的来源。** 沙箱删除的最终结果由 worker 写入 `agent_admin_audit_outcomes`。管理员恢复
   重试（`retry_resumed`）时会清掉已有结果，所以这张表是"当前已知结果"的投影，可以更新，
   不受只追加约束；审计行本身仍然只追加。compute 操作的结果不复制，读取时直接 join
   `agent_compute_operations` 的状态。worker 的 `archived` 和非终态 `failed` 步骤不再写任何
   审计数据，它们本来就记录在 `agent_sandbox_events` 里。
5. **名字快照。** 写入时查账号表：优先登录名，没有则依次用邮箱、手机号、`user_name`。查不到时
   （例如 erase 这一行是在账号身份删除之后写的），回退到审计日志里该账号最近一次记下的名字。
   因此审计行现在可能存有邮箱或手机号。`docs/developer/local-accounts.md` 原本写的"审计行不记录
   邮箱"已经同步改写。
6. **写入失败不影响操作。** 所有写入点都经过 `storage/_sql/admin_audit.py` 的
   `insert_audit_row`，在调用方事务的 savepoint 里写入。失败时只回滚 savepoint，记录
   `agent_admin_audit_write_failed` 日志，并计入 `agent_admin_audit_write_failures`。
   原来 sandbox 的归档、停止、删除，以及账号变更"审计写失败就整体回滚"的行为都已改掉，
   相应测试按新行为改写。`log_admin_action` 保留为 HTTP 层的入口。
7. **注册表不逐字段校验 `details`。** 改用 AST 测试（`tests/unit/test_audit_registry.py`）扫描
   所有写入调用，确认 action 都已注册，并核对迁移里冻结的回填表和注册表完全一致。前端用的
   `apps/frontend/src/app/(agents)/admin/audit/actions.json` 由同一个测试保证与注册表同步；
   前端测试再检查其中每个 action 都有描述句子。运行时遇到未注册的 action，照样写入，
   `target_type` 记为 `unknown`。
8. **`agent.compute.connection.validate` 不再记录。**
9. **只追加触发器推迟到下一个发布。** 原因是上一个发布在擦除账号时仍会 `DELETE` 审计行；
   如果这次就加触发器，滚动发布窗口内的擦除操作会失败。
10. **入口。** 用户详情页加了"查看审计记录"链接（`/admin/audit?user=<id>`），沙箱详情页加了
    "Audit history"。主机页的组件在 eslint 复杂度名单里，没有改动；主机的历史从审计页
    每一行的 "Show only this" 进入。按用户查看时，页面默认包含该账号自己的操作。

