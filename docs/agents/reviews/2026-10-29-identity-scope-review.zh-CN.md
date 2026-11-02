# PR #1030 身份范围评审：手机号登录、login_name、多账号与敏感操作确认

- **日期**：2026-10-28
- **评审者**：Claude Opus 5.5（`claude-opus-5-5`），reasoning effort 为本会话默认档位（委托人未单独指定）
- **评审对象**：Cloud Agent PR #1030 `codex/feat/independent-agent-accounts`，head `cd0842b23332821bbf588fa16aad29536197ed98`（+43,472 / −1,981，352 文件，未合并；`gh` 报告的 mergeable 为 `UNKNOWN`）
- **上游对照**：HybridInference `dev` @ `76bf431a98dbde223d67afb865985294e2e39abd`（2026-10-28，已通过 HTTPS 取到最新）
- **读代码的方式**：只读 worktree `~/pr1030-review-cd0842b2`（PR head）和 `~/hi-review-76bf431a`（上游）。`dev` 工作区没有改动，没有 push，也没有建 PR。

下文的路径都相对各自仓库根目录。标注 **【已确认】** 的是我读代码确认的；标注 **【推断】** 的是根据代码做的推理或常识判断，没有运行验证。

## 0. 实际读过的文件

**Cloud Agent（PR head `cd0842b2`）**

- `migrations/versions/0107_local_accounts.py`（全文）
- `apps/backend/cloud_agent/api/auth.py`（全文）
- `apps/backend/cloud_agent/api/_local_auth.py`（全文）
- `apps/backend/cloud_agent/application/accounts.py`（全文）
- `apps/backend/cloud_agent/domain/accounts.py`（全文）
- `apps/backend/cloud_agent/accounts/{config,tokens,session_cookie,bootstrap_admin,directory}.py`（全文）
- `apps/backend/cloud_agent/accounts/mail.py`（只读了 `MailKind` 部分）
- `apps/backend/cloud_agent/ports/accounts.py`（方法清单）
- `apps/backend/cloud_agent/storage/repositories/accounts.py`（第 1–80、190–249、400–440、560–600 行）
- `apps/backend/cloud_agent/storage/_sql/accounts.py`（全文）
- `apps/backend/cloud_agent/storage/repositories/model_connections.py`（第 60–82 行，以及 `local_access_ended` 的调用点）
- `apps/backend/cloud_agent/identity_namespace.py`（全文）
- `apps/backend/cloud_agent/api/session.py`（grep 了 local/logout/switch 相关段落）
- `apps/backend/cloud_agent/api/deps.py:749-774`（`get_client_ip`）
- `apps/backend/cloud_agent/application/teams.py:78-95`
- `apps/frontend/src/components/features/auth/{LoginPageClient,SessionSwitchWatcher}.tsx`
- `docs/agents/specs/2026-10-27-independent-accounts-and-waitlist-design.zh-CN.md`（全文）
- `AGENTS.md` §1、§7；`README.md`（license 与 self-hosted 的说明）
- `tests/` 中 grep 了 `login_email` / lockout；`tests/integration/storage/test_identity_namespace.py` 中 grep 了列名
- `dev` 上的提交 `9c2963631`（#685，通过 Gateway 账号选择器切换账号）的提交说明

**HybridInference（`76bf431a`）**

- `apps/backend/serving/utils/login_rate_limit.py`（全文）
- `apps/backend/serving/storage/phone_auth.py`（第 1–140、192–356 行）
- `apps/backend/serving/utils/phone_auth.py`（全文）
- `apps/backend/serving/servers/routers/phone_auth_routes.py`（第 85–190 行，其余部分只 grep 过）
- `apps/backend/serving/storage/account_schema.py`（grep 了身份相关列）
- `apps/backend/serving/auth/browser_sessions.py`（第 1–46 行）

### 没能验证的部分

1. 没有运行任何测试，也没有运行 `make check`、`make test-db-local` 等 gate。
2. 没有核实 #1030 与 `dev` 的具体冲突。`gh` 返回 `mergeable: UNKNOWN`。
3. 委托人给出的上游手机功能行数（约 3,070 行）没有复核：沙箱不允许对上游路径执行 `wc`。上游的 `PhoneAuthForm.tsx`、`phone-countries.ts`、`tencent_cloud/*`、turnstile 也没有读。
4. 第 2.2 节说 `_email_key` 与 `normalize_email` 的规范化结果可能不同，这一点我没有实际运行（执行 Python 的请求未获批准）。
5. 中国大陆短信合规（企业实名、签名报备、模板审核）和号码回收周期属于一般行业常识，没有在本次查证当前法规原文。
6. 除身份相关模块外，PR 的其余约 300 个文件没有读。

---

## 1. 结论摘要

| 项 | 结论 | 一句话理由 |
|---|---|---|
| (a) 手机号 + 验证码登录 | **可以随后做**，以部署方显式开启的可选插件形式提供；首版不做 | 这是部署侧的合规与费用依赖，默认必须关闭；而且它给高价值账号引入了较弱的单一因子 |
| (b) `login_name` 账号（无邮箱） | **紧接 #1030 之后第一个做**，形式是"运维用 CLI 预置账号"，不开放公开注册 | 目前 local 模式**没有 SMTP 就完全无法使用**，这是自托管的真实门槛 |
| (c) 同浏览器多账号同时登录 | **可以随后做，优先级最低** | Cloud Agent 已经决定"一个浏览器一个会话 + 切换"（#685），local 模式也已经能"登录即切换" |
| (d) 敏感操作确认 | **需求首版就缺，但不应绑死在手机 OTP 上**：先做"重新认证（step-up）"框架和自助改密码；手机 OTP 只是以后的一种因子 | 现在没有任何自助改密码、改邮箱、删号入口，这才是首版真正的空白 |

**对产品负责人判断的回应**：

- **部分正确**。设计文档第 15 行写的方向是"复用 HybridInference 已有登录系统的代码和交互"；第 117 行规划的"登录身份"包含"**登录类型**、唯一规范化标识、验证状态"。但实现把身份做成了 email 专表（`agent_local_password_logins`），并把 `email_verified` 硬编码进至少 **7 处**授权判断（见 2.1）。这确实是设计和实现之间的偏差，以后扩展身份类型时必须改掉。
- **部分不对**。"这些功能都应该支持"如果意味着"放进首版 / 放进 #1030"，我不同意：
  1. #1030 已经 43k 行、待合并、有冲突。再加约 3k 行带外部合规依赖的代码，评审质量和合并风险都会明显变差。
  2. 本产品的目标用户是**开发者**。他们要把结果发 PR 到 GitHub/GitLab（`AGENTS.md:8-10`），这类账号本身要求邮箱，所以"必须有邮箱"对这个人群几乎不构成门槛。
  3. 真正卡住自托管者的不是"用户没有邮箱"，而是"**运维没有 SMTP**"。这个问题 (b) 就能解决，不需要手机号。
  4. 手机号默认开启对开源自托管不可行：每个部署者都要有短信供应商合同，中国大陆还要过企业资质和审核。上游自己也是默认关闭的（`SMS_AUTH_ENABLED`）。
- **反方论点里成立的部分**：在中国大陆，手机号是实名的，伪造成本比邮箱高得多。对"会跑任意代码、有公网出口的沙箱平台"来说，这是很有价值的**反滥用 / 准入信号**。所以我建议把手机号首先当作"**已验证的联系属性**"（可以作为审批参考，或被部署方设为申请前提），其次才是登录方式。

---

## 2. 证据

### 2.1 现有模型其实已经"账号 / 登录凭据"分表，但 email 渗透进了授权判断 【已确认】

`migrations/versions/0107_local_accounts.py:34-55`：`agent_local_accounts` 里没有邮箱，邮箱和密码在 1:1 的 `agent_local_password_logins` 里（主键 `account_id`）。所以从 schema 上说，**新增身份类型是加表，不是改 `agent_local_accounts`**，比委托人描述的"单身份 NOT NULL"更容易演进。

但 email 已经渗进这些地方：

| 位置 | 依赖 |
|---|---|
| `storage/repositories/accounts.py:50-54` | `_ACCOUNT_FROM` 用 **INNER JOIN** `agent_local_password_logins`。没有这行的账号，`get_account` 读不到 |
| `storage/repositories/accounts.py:66-68` | `_ACCESS_ENDED_SQL` 判断 `l.email_verified_at IS NULL` |
| `storage/_sql/accounts.py:18-25` | `local_access_ended()`：`EXISTS (... JOIN password_logins ... WHERE ... OR email_verified_at IS NULL)` |
| `storage/repositories/model_connections.py:69-80` | 准入判断再写一遍 `l.email_verified_at IS NOT NULL` |
| `storage/repositories/accounts.py:422-426` | `create_session` 同时要求 `password_hash` 没变和 `email_verified` |
| `domain/accounts.py:109-120, 128, 169-174` | `permissions_for`、`may_sign_in`、`_approve`、`_grant_admin` 都判断 `email_verified` |
| `accounts/bootstrap_admin.py:51, 103-107` | 第一位管理员必须已验证邮箱，并且按邮箱查找 |
| `accounts/directory.py:101-106` | `user_email()` 返回 `str`，被 Profile 和团队名册使用（`application/teams.py:78-95`） |

**一个潜在的"失败即放行"（fail-open）陷阱** 【已确认 + 推断】：`local_access_ended()`（`_sql/accounts.py:18-25`）是"存在一个已结束访问的本地账号"这种写法，里面 INNER JOIN 了 `password_logins`。今天每个账号都在同一事务里带一行 password_login（`repositories/accounts.py:209-237`），所以结果正确。但只要将来出现**没有 password_login 行的账号**（纯手机号、纯 login_name），JOIN 就没有结果，`EXISTS` 为假，"访问未结束"成立。于是 `residency.py:145`、`model_connections.py:401` 上的撤权栅栏会**放行**一个已被撤销或停用的账号。相比之下，`model_connections.py:69-80` 和 `_ACCOUNT_FROM` 遇到同样情况会拒绝（fail-closed）。所以扩展身份时**必须先把这几个判断统一并改成 fail-closed**，否则会引入撤权绕过。

### 2.2 登录限流缺陷：**成立，而且比描述的更严重** 【已确认】

`api/auth.py:112-119` 定义了三个桶，`api/_local_auth.py:297-306` 的 `enforce_rate_limits`：

```python
for bucket, key, window_s, limit in rules:
    wait = max(wait, await accounts.hit_rate_limit(...))
```

`hit_rate_limit`（`storage/repositories/accounts.py:570-599`）**无条件** `INSERT ... ON CONFLICT DO UPDATE SET hits = hits + 1`，然后才和 limit 比较。循环不会因为前一个桶超限就停下。结果是：

1. **单个 IP 就能锁死账号**。攻击者在 15 分钟内对 `victim@x.com` 发 50 次请求：第 6 次起 `login_email_ip` 已经超限，第 21 次起 `login_ip` 也超限，但每一次请求仍然给 `login_email` 加 1。到第 51 次时，`login_email` 对所有 IP 都超限。账号主人在任何地方都登不上，直到固定窗口结束；攻击者每个窗口重复一次，就能长期锁死。委托人说"要 50 次错密码"，实际连"错密码"都不需要，**被拒绝的请求同样计数**。
2. 计数发生在验密码**之前**（`auth.py:112` 先于 `:121`），所以**成功登录也计数**。
3. 测试里没有任何针对这些桶的用例（`tests/` 中 grep `login_email` 没有结果）。

上游的做法（`hybridInference: serving/utils/login_rate_limit.py:158-182`）：两个 per-address 桶总是计数；只有两个都放行的尝试才进入 account-wide 桶，并且 account-wide 桶只记录被放行的尝试。另外，"认识的设备"（device cookie）只走自己的桶（`:148-156`），不受别人影响。这就是为什么上游注释说"填满 account 桶需要十个地址"。

**正确修法**（第一部分应该进 #1030）：

```python
async def enforce_login_limits(request, ident: LoginIdentifier) -> None:
    ip = client_bucket(request)
    # 1) per-address 桶：总是计数；任何一个超限 → 429，且不碰 account 桶
    await enforce_rate_limits(request, [
        ("login_ident_ip", f"{ident.key}\x00{ip}", _FIFTEEN_MIN, 5),
        ("login_ip", ip, _HOUR, 20),
    ])
    # 2) 只有通过了 per-address 的尝试才进入 account-wide 桶
    await enforce_rate_limits(request, [("login_ident", ident.key, _FIFTEEN_MIN, 50)])
```

改完后，单个 IP 最多给 account 桶贡献 5 次，要 10 个以上的 /32（IPv6 是 /64）才能锁死账号。要彻底解决分布式锁号，需要第二步：**已识别设备豁免**。成功登录过的浏览器拿到一个 `__Host-` HttpOnly 的设备凭据（服务端只存 keyed hash），之后这个浏览器的尝试只计入自己的设备桶。这是独立的小 PR。

**相关的部署风险** 【已确认 + 推断】：`get_client_ip`（`api/deps.py:765-774`）只有在 `AGENT_TRUST_PROXY_HEADERS` 打开时才读取 XFF。如果部署在反向代理后面却没开这个开关，所有用户共用代理地址，`login_ip` 的 20 次/小时就变成**整个部署的登录上限**。运维文档必须强调这一点，或者在启动时检测"所有请求都来自同一个私网地址"并告警。

**键规范化的小问题** 【推断，未运行验证】：限流键用 `_email_key = strip().lower()`（`auth.py:61-64`），数据库唯一性用 `email_validator` 的 `normalized.lower()`（`application/accounts.py:82-93`）。两者对 IDN 域名、Unicode 组合字符可能给出不同结果，攻击者可以用同一邮箱的几种写法得到几个独立的桶。倍数有限，严重性低。但多身份之后，**限流键应该统一使用解析后的规范化标识**；输入无法解析时退回 `raw:` 前缀的键。

### 2.3 为什么 (b) 比 (a) 更急：local 模式离不开 SMTP 【已确认】

- `signup_enabled()` 要求 `account_mailer(request).configured` 并且 Web base URL 合法（`_local_auth.py:243-250`）。
- 登录要求 `email_verified`（`application/accounts.py:169-170`），而 email 只能通过邮件里的链接验证。
- `bootstrap_admin` 要求账号已经验证邮箱（`bootstrap_admin.py:51-52`），管理员动作里也没有"标记已验证"。

结论：**没有可用 SMTP 的部署（内网、家庭实验室、评估用）里，任何人都无法登录 local 模式，连第一位管理员都建不出来**。对一个 MIT 开源、自托管的项目（`README.md:4, 9`）来说，这比"有些用户没有邮箱"更真实。

### 2.4 为什么 (c) 优先级低 【已确认】

- `dev` 上的 #685（提交 `9c2963631`）说明里写得很清楚："*Agents stays one session per browser; this lets a person change which account that is*"。也就是说，Gateway 模式下已经有意选择"单会话 + 切换"，并配好了 `BroadcastChannel` 广播和 `SessionSwitchWatcher`（`apps/frontend/src/components/features/auth/SessionSwitchWatcher.tsx`），保证其他标签页切换后回到首页。
- local 模式已经是"登录即替换"：`api/auth.py:122-125` 登录成功后会撤销这个浏览器原有的会话。所以"换账号"目前只是"退出后再登录"，和 Gateway 模式在体验层面一致。
- 同时登录多个账号对 Cloud Agent 的代价比对上游更高：终端 WebSocket、驻留进程、任务页都按 owner 绑定，"当前账号"变化时，已打开的长连接属于谁需要逐条论证。

### 2.5 手机号作为身份的安全性 【推断，依据为通用安全常识与上游代码】

- Cloud Agent 账号的价值很高：它控制沙箱，沙箱里有 owner 的 Secrets、GitHub App 推送 token、已登记的 SSH 服务器（`AGENTS.md:252-277`）。只用短信 OTP 作为唯一因子（无密码），会让 SIM 换卡、号码回收、短信劫持直接变成账号接管。邮箱 + 密码至少要求攻破邮箱或猜中密码，而且邮箱接管通常比运营商社工更难。
- 上游的一个具体侧信道（读代码推断，未实测）：`phone_auth_routes.py:159-183` 只对"有资格"的号码**同步**调用短信供应商（`run_in_threadpool(send_verification_sms, ...)`），没资格的号码直接返回。响应时间的差异会泄露号码是否已注册。另外 `phone_auth.py:86-93` 的 `phone_auth_attempts` 存的是**明文手机号**，与 Cloud Agent "限流键只存 keyed hash"的约定（`0107:101-102`、`tokens.py:64-66`）相冲突。这也说明**不能照抄**。

---

## 3. Cloud Agent 自己的设计

### 3.1 数据模型：`0108_local_login_identities`

**原则**：账号是 owner，身份是"如何证明你是这个 owner"，密码是一种凭据。三者分开，符合规格第 116–117 行原本的意图，也为 A3（Gateway → local 迁移）预留了位置。

```sql
-- 登录标识：一类一行，类型之间的格式由 CHECK 强制互斥
CREATE TABLE agent_local_login_identities (
    id TEXT PRIMARY KEY CHECK (id ~ '^[0-9A-HJKMNP-TV-Z]{26}$'),
    account_id TEXT NOT NULL REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('email', 'phone', 'login_name')),
    value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 320),          -- 展示形式
    value_normalized TEXT NOT NULL,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (kind, value_normalized),
    UNIQUE (account_id, kind),            -- 首版：每类最多一个；以后要多邮箱再放开
    CHECK (
        (kind = 'email'      AND value_normalized = lower(value_normalized)
                             AND position('@' IN value_normalized) > 1)
     OR (kind = 'phone'      AND value_normalized ~ '^\+[1-9][0-9]{7,14}$')
     OR (kind = 'login_name' AND value_normalized ~ '^[a-z][a-z0-9._-]{2,31}$')
    )
);
CREATE INDEX idx_agent_local_login_identities_account ON agent_local_login_identities (account_id);

-- 密码凭据：可选
CREATE TABLE agent_local_password_credentials (
    account_id TEXT PRIMARY KEY REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
    password_hash TEXT NOT NULL CHECK (left(password_hash, 10) = '$argon2id$'),
    password_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

要点：

1. **格式互斥写进 CHECK**：email 必须含 `@`；phone 必须是以 `+` 开头的 E.164；login_name 必须以字母开头，且不含 `@` 和 `+`。上游"login_name 不含 `@`"只是注释里的约定，这里成为数据库约束。这样即使限流键忘了加类型前缀，不同类型也不会撞。但仍然要加前缀（见 3.2），用两道防线。
2. **数据迁移**（`upgrade()`，同一事务内）：
   - `INSERT INTO agent_local_login_identities SELECT <新ULID>, account_id, 'email', email, email_normalized, email_verified_at, created_at FROM agent_local_password_logins`
   - `INSERT INTO agent_local_password_credentials SELECT account_id, password_hash, password_changed_at FROM agent_local_password_logins`
   - 然后 `DROP TABLE agent_local_password_logins`。

   ULID 不要在 SQL 里拼。建议 `id` 改为 `TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text`，或者用 `(kind, value_normalized)` 作自然主键，迁移脚本就最简单。**必须保留数据**：README 第 53 行说明这个项目"没有版本化 release，配置和 API 可能随提交变化"，自托管者可能直接跑 `dev`。
3. **`downgrade()`**：只有当每个账号都正好有"一个 email 身份 + 一份密码"时才能还原，否则 `RAISE`，拒绝降级。不能悄悄丢掉手机号账号。
4. **不变式"每个账号至少有一个已验证身份才能登录"**：不用 CHECK 表达，由仓储在 `agent_local_accounts` 行 `FOR UPDATE` 下维护（沿用 `repositories/accounts.py:3-7` 的锁顺序：先锁账号行）。解绑最后一个身份时拒绝。
5. **`identity_namespace` 约束** 【已确认】：`_has_owner_data`（`identity_namespace.py:25-37`）把所有以 `agent_` 开头、含 `user_id` / `target_user_id` 列的表都当作 owner 数据。`0107` 的文档字符串（第 7–8 行）因此刻意不用 `user_id`。新表（身份表、OTP 表、浏览器会话表）**一律用 `account_id`**。现有测试没有覆盖这条约定（`test_identity_namespace.py` 只建了 `agent_threads`、`agent_future_connections`），应新增一条契约测试：断言 `agent_local_%` 表都不含这两个列名。否则以后某个 OTP 表加了 `user_id`，会被当作 owner 数据，干扰 A3 迁移时的空库判断。
6. **A3 的协同** 【推断】：规格第 138–141 行要求 `(legacy_issuer, legacy_subject) -> local_account_id` 映射。它可以作为 `kind = 'gateway_legacy'` 的一类身份放进同一张表（`value_normalized = issuer || '\x00' || subject`），从而复用唯一性和"不按邮箱合并"的规则。但它**不能用来登录**，只用来证明"这个本地账号继承了那个 owner"。这一点需要在 A3 设计里单独决定。

**授权判断统一**（与 0108 同一个 PR）：

```sql
-- 唯一事实来源，取代 7 处硬编码的 email_verified
CREATE VIEW agent_local_account_access AS
SELECT a.id AS account_id,
       (a.status = 'active'
        AND EXISTS (SELECT 1 FROM agent_local_login_identities i
                    WHERE i.account_id = a.id AND i.verified_at IS NOT NULL)) AS may_sign_in,
       (a.status = 'active' AND m.status = 'approved'
        AND EXISTS (SELECT 1 FROM agent_local_login_identities i
                    WHERE i.account_id = a.id AND i.verified_at IS NOT NULL)) AS has_use
FROM agent_local_accounts a JOIN agent_local_admissions m ON m.account_id = a.id;
```

- `local_access_ended(col)` 改写为 `EXISTS (SELECT 1 FROM agent_local_account_access x WHERE x.account_id = col AND NOT x.has_use)`。Gateway 模式没有本地账号，结果恒为假，行为不变。本地账号缺任何一行时 `has_use` 为假，**fail-closed**。
- `AccountRecord.email: str` 改为 `email: str | None`、`login_name: str | None`、`phone_masked: str | None`、`identity_verified: bool`。`permissions_for` / `may_sign_in` 只看 `identity_verified`，Python 和 SQL 的规则写一份契约测试互相核对。
- `user_email()` 改为 `user_display()`，按 email → login_name 的顺序取，**不返回手机号**：团队名册不应暴露成员手机号。Profile 只显示自己的掩码号码。

### 3.2 登录标识抽象与限流分桶

```python
@dataclass(frozen=True)
class LoginIdentifier:
    kind: Literal["email", "login_name", "phone", "raw"]
    normalized: str
    @property
    def key(self) -> str:            # 限流键 = 类型前缀 + 规范化值
        return f"{self.kind}:{self.normalized}"

def parse_password_identifier(raw: str) -> LoginIdentifier:
    s = raw.strip()
    if "@" in s:
        try: return LoginIdentifier("email", normalize_email(s)[1])
        except InvalidEmail: return LoginIdentifier("raw", s.lower())
    if LOGIN_NAME_RE.fullmatch(s.lower()):
        return LoginIdentifier("login_name", s.lower())
    return LoginIdentifier("raw", s.lower())
```

- **密码登录**只接受 email 和 login_name，字段改名为 `identifier`，API 保留 `email` 作为兼容别名。**手机号不参与密码登录**：手机号账号首版无密码，走 OTP 端点。这样密码路径和 OTP 路径的限流、枚举防护完全分开。
- 限流键 = `rate_limit_key(bucket, ident.key)`。`tokens.py:64-66` 已经把 bucket 名混进 HMAC，类型前缀让同一个桶内不同类型也不可能冲突。这条正确性由**构造**保证，不依赖"login_name 不含 `@`"这种格式巧合。数据库 CHECK 再兜一层。
- `raw:` 键：输入无法解析时也要计数，否则攻击者可以用畸形输入绕过 per-identifier 桶而只消耗 `login_ip`。
- **账号不存在时的时序防护**：保留现有 `verify_password(password, None)` 的假校验（`application/accounts.py:160-167`）。login_name 查不到时同样执行。OTP 路径见 3.4。
- **枚举防护**：密码路径保持"Invalid email or password"一种回答（`_local_auth.py:77`），文案改为"账号或密码错误"。login_name 注册只由运维 CLI 完成，因此不存在"用户名已被占用"这种公开的枚举面。

### 3.3 会话：多账号怎么做（以后的阶段）

**不建议**用 cookie 数组：每个元素都是长期凭据，cookie 体积、`__Host-` 的单值语义、按条撤销都会变复杂。

**建议**采用与上游同构、但用 Cloud Agent 自己实现的"浏览器会话"：

```sql
CREATE TABLE agent_local_browser_sessions (
    id TEXT PRIMARY KEY,
    token_hash BYTEA NOT NULL UNIQUE CHECK (length(token_hash) = 32),
    active_session_id TEXT REFERENCES agent_local_sessions (id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ
);
ALTER TABLE agent_local_sessions
    ADD COLUMN browser_session_id TEXT REFERENCES agent_local_browser_sessions (id) ON DELETE CASCADE;
-- 每个浏览器会话最多 5 个账号，由仓储在浏览器会话行 FOR UPDATE 下检查
```

- cookie 仍然只有一个：`__Host-cloud_agent_local_session`，HttpOnly，SameSite=Lax，Path=/，属性完全不变。值变成浏览器会话令牌，用新前缀（如 `calb1.`）区分。旧的 `cals1.` 值第一次出现时就地升级，旧值作废（上游 `browser_sessions.py:15-17` 的思路）。
- **每个成员会话仍然是 30 天固定期限、不续期、可单独撤销**。浏览器会话的 `expires_at` 等于创建时间加 30 天，同样固定。加入新账号**不会**延长已有成员的期限。
- 每个请求都在服务端解析当前成员：浏览器会话 → `active_session_id` → 成员会话必须仍有效 → 账号的 `may_sign_in`。成员被撤销（重置密码、停用）后，**不自动切到另一个账号**，而是返回 401 加 `reason=choose_account`，由前端显示选择器。自动切换会让用户在不知情时以另一个身份操作。
- 端点：`GET /v1/auth/accounts`（只返回 id、显示名、掩码标识，没有任何令牌）、`POST /v1/auth/switch {account_id}`、`POST /v1/auth/logout {scope: "current" | "all"}`。全部走现有的跨站写入保护和 `require_json`。
- **切换会话归属的边界**：已打开的终端 WebSocket、SSE 在**打开时**绑定成员账号，切换后不迁移，旧连接要么继续以旧账号运行，要么被关闭。我建议关闭，并复用 `SessionSwitchWatcher` 把所有标签页带回首页。这一点要在设计文档里写明并测试。
- "登录 CSRF"：把攻击者账号加进受害者的浏览器会话，比整体替换更隐蔽，因为受害者原账号还在列表里。所以 `signup` / `login` 继续强制跨站写入检查（`auth.py:6-9` 的现有逻辑）。切换器里要醒目显示当前账号。

**在此之前的过渡方案**：local 模式加一个"切换账号"菜单项 = 退出 + 跳到 `/login?login_hint=<掩码标识>`。现有后端已经支持（`auth.py:122-125`），前端改动很小。

### 3.4 OTP 存储

```sql
CREATE TABLE agent_local_otp_challenges (
    id TEXT PRIMARY KEY,                               -- 返回给前端的 challenge_id（ULID）
    purpose TEXT NOT NULL CHECK (purpose IN ('phone_login', 'phone_bind', 'step_up')),
    channel TEXT NOT NULL CHECK (channel IN ('sms', 'email')),
    destination_hash BYTEA NOT NULL CHECK (length(destination_hash) = 32),  -- keyed hash，不存明文号码
    account_id TEXT REFERENCES agent_local_accounts (id) ON DELETE CASCADE, -- 注意：不是 user_id
    code_hash BYTEA NOT NULL CHECK (length(code_hash) = 32),
    state TEXT NOT NULL CHECK (state IN ('reserved', 'sent', 'consumed', 'failed')),
    attempts SMALLINT NOT NULL DEFAULT 0,
    max_attempts SMALLINT NOT NULL DEFAULT 5,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX agent_local_otp_open ON agent_local_otp_challenges
    (purpose, destination_hash, coalesce(account_id, '')) WHERE state IN ('reserved', 'sent');
CREATE INDEX agent_local_otp_expiry ON agent_local_otp_challenges (expires_at);
```

- `code_hash = HMAC(derived_key("otp"), "otp-v1\0" ‖ challenge_id ‖ purpose ‖ destination ‖ account_id ‖ code)`。密钥用现有 `derived_key` 的 HKDF 用途隔离（`accounts/config.py:95-111`），**不像上游那样借用 JWT secret**（上游 `utils/phone_auth.py:24-27`）。
- **限流复用现有的 `agent_local_auth_rate_limits`**：Postgres 固定窗口，多副本共享，键已经是 keyed hash，不需要另建 attempts 表，也不需要上游那种基于 `pg_advisory_xact_lock` 和明文号码的计数。新桶：

  | 桶 | 键 | 窗口 / 上限 |
  |---|---|---|
  | `otp_send_dest_cooldown` | 号码 | 60s / 1 |
  | `otp_send_dest` | 号码 | 1h / 5；1d / 10 |
  | `otp_send_ip` | IP 桶 | 1h / 10；1d / 30 |
  | `otp_send_prefix_day` | 国家码 | 1d / 部署配置 |
  | `otp_send_global_day` | 常量 | 1d / 部署配置（**费用上限**） |
  | `otp_verify_dest` | 号码 | 15min / 10 |
  | `otp_verify_ip` | IP 桶 | 1h / 30 |

- **爆破算术**：6 位码、每码 5 次尝试，而且**每次尝试都绑定在某次发送上**。所以每个号码每天最多 10 次发送 × 5 次 = 50 次猜测，日成功率约 5×10⁻⁵，一年约 1.8%。如果放任 `verify` 与 `send` 解耦，只按 15 分钟 10 次计算，一年会到约 35%。**必须让 challenge 的 `attempts` 成为主约束**，verify 桶只作辅助。
- **消费是原子的**：同一事务内 `SELECT ... FOR UPDATE`，然后 `attempts + 1`，`compare_digest`，成功时 `state = 'consumed'`。错误尝试也要提交。如果涉及账号（bind / step_up），先按现有锁顺序锁账号行。
- **"先预留后发送"**：先写 `reserved`，供应商确认后才改为 `sent`，失败改为 `failed`，与上游一致。这样"发出去但没记录"的码不可能被使用。
- **防枚举**：`phone_login` 采用"验证码登录 / 注册一体"。任何号码在配额内都会收到码，响应**立即**返回，短信在 `BackgroundTasks` 中发送（和现有邮件同样的做法，`_local_auth.py:266-268`）。响应时间与号码是否注册无关。验证通过后：已有账号就登录；没有账号且部署开放注册就创建，否则显示"该部署未开放注册"。只有号码持有人能看到这个区别。
- **代价**：向任意号码发短信会招来 SMS pumping（短信轰炸 / 计费欺诈）。所以开启手机登录必须同时配置 ① 号码前缀白名单（默认只允许部署方声明的国家码），② 全局日上限，③ 人机验证。三项**缺任何一项都拒绝启动手机登录**。

### 3.5 短信 provider：一个接口 + 一个实现，不做注册表

仿照现有 `AccountMailer`（`accounts/mail.py`：`configured` + `send`）：

```python
class SmsSender(Protocol):
    configured: bool
    async def send_code(self, e164: str, code: str, *, locale: str) -> None: ...

# accounts/sms.py
def sms_sender_from_env() -> SmsSender:
    match os.environ.get("AGENT_SMS_PROVIDER", "disabled"):
        case "disabled": return DisabledSms()
        case "tencent":  return TencentSms.from_env()   # AGENT_SMS_TENCENT_*
        case other: raise LocalAuthNotConfigured(f"unknown AGENT_SMS_PROVIDER {other!r}")
```

- 不引入上游的 `delivery` 注册表：Cloud Agent 只有 SMTP 一种投递，为了第二种去建注册表属于过度设计。等出现第三种（例如 Twilio）时再提取，代价也很小。
- 第一个真实实现建议选腾讯云，原因是 FreeInference 的部署在中国大陆【推断】。同时提供 `FakeSms`（测试用，`app.state.sms_sender` 可替换，与 `app.state.account_mailer` 同样的模式，`_local_auth.py:222-228`）。
- **不建议**做通用 webhook 实现：它会把验证码明文送到任意 URL，增加一个难以审查的出口。
- **AGENTS.md §7**（第 252–255 行）：短信供应商的 SecretId / SecretKey 属于平台长期凭据，只能存在于 API 进程环境中。要加一条测试，断言它们不会出现在 runner、host claim、沙箱的环境变量白名单里。供应商的异常信息可能带凭据或验证码，只记录类型，不记录消息（上游 `routes:174-176` 有同样的处理）。
- 人机验证同样只做一个接口，第一个实现可选 Turnstile 或腾讯验证码。**Cloudflare Turnstile 在中国大陆的可达性不稳定**【推断】，所以不能硬编码 Turnstile。

### 3.6 敏感操作确认：先做 step-up 框架

```
POST /v1/auth/reauth {method: "password", password}          → 设置短期 step-up 标记（5 分钟）
POST /v1/auth/reauth {method: "otp", challenge_id, code}     → 同上（手机阶段之后才有）
POST /v1/account/password   {new_password}            需要 step-up；撤销其他会话
POST /v1/account/email      {new_email}               需要 step-up；验证新邮箱；通知旧邮箱
POST /v1/account/phone      {challenge_id, code}      需要 step-up；绑定已验证号码
DELETE /v1/account                                    需要 step-up；走 terminate_owner_access 流程
```

- step-up 标记存在**会话行**上（`agent_local_sessions.reauth_at`），不另发 cookie，也不放进前端可读的位置。
- 可用的 step-up 方式取决于账号拥有的因子：有密码就用密码；只有手机就用手机 OTP（`purpose = 'step_up'`，只发到**本账号已验证的号码**，对应上游 `OWNER_PURPOSES` 的语义，`routes:164-168`）；login_name 账号只有密码。
- **删除账号**必须单独设计，不要附带在这里。规格 §4（第 101–108 行）要求删除时撤销模型连接、销毁密文、停止驻留进程，这和 `agent_local_access_terminations` 的终止流程是同一类复杂度。

### 3.7 前端

`GET /v1/auth/config`（`AuthConfigResponse`）扩展为：

```json
{
  "mode": "local",
  "signup_enabled": true,
  "methods": {
    "password": { "identifiers": ["email", "login_name"] },
    "phone_otp": { "enabled": false, "signup": false, "prefixes": ["+86"],
                   "captcha": { "provider": "tencent", "site_key": "..." } }
  },
  "multi_account": false
}
```

- `LoginPageClient.tsx:32-37` 的结构保持不变：`gateway` → `GatewayLoginPage`；`local` → `LocalLogin`。`LocalLogin` 内部根据 `methods` 渲染：只有密码时就是今天的表单，输入框标签改为"邮箱或用户名"；`phone_otp.enabled` 时增加"手机验证码"页签。不开启的方式**完全不渲染**，不显示灰色占位。
- 手机组件只做国家码选择（仅列出 `prefixes`）、号码输入、发送冷却倒计时、6 位码输入。E.164 规范化以服务端为准，前端只做格式提示。不要搬上游 260 行的全量国家表。
- 新增 `/settings/security`：改密码、绑定或更换邮箱、绑定手机、会话列表（撤销其他会话）。删除账号等设计完成后再放进来。
- 中英文 i18n 都要写；中文文案遵守"读起来像人写的中文"的要求。

---

## 4. 落地路径

> 本仓库要求设计文档和实现放在同一个 PR，不单独提交文档 PR。下面每个阶段都在同一个 PR 里附上对应的设计文档更新。

### 阶段 0：仍然放进 #1030（小改动，不扩大范围）

| 改动 | 理由 | 验收 |
|---|---|---|
| 修正登录限流：先过 per-address 桶，再进 account 桶（见 2.2） | 现在单个 IP 就能锁死任意账号，属于合并前缺陷 | 新单元或集成测试："单 IP 打 60 次后，账号主人从另一个 IP 用正确密码能登录"；"10 个 IP 各打 5 次后 account 桶触发"；`make check`、`make test-db-local` |
| 把 `email_verified` 的 7 处判断收敛为一处，并让 `local_access_ended` fail-closed（只重构，不改 schema） | 为扩展做准备，消除 2.1 的 fail-open 陷阱 | Python 与 SQL 规则的契约测试；缺 login 行的账号被栅栏拒绝的测试 |
| 限流键改用规范化后的标识 | 消除同一邮箱多种写法得到多个桶 | 单元测试 |
| 新增契约测试：`agent_local_%` 表不含 `user_id` / `target_user_id` | 保护 namespace 守卫的约定 | `make test-db-local` |
| 规格第 22 行改为"推迟到后续阶段，见 §X 演进计划"，并修正第 117 行"登录类型"与实现的偏差说明 | 文档与代码一致 | `make docs-verify` |

**前两项我认为是合并的必要条件**，后三项强烈建议做。

### 阶段 1（#1030 合并后的第一个 PR）：多身份 schema + login_name + 自助改密码

- `0108_local_login_identities`：数据迁移、授权视图、`AccountRecord` 变更、`user_display()`。
- 运维 CLI：`python -m cloud_agent.accounts.provision --login-name alice [--email ...] [--approve] [--admin]`。密码从 TTY 或 stdin 读取，**不经过 argv**（AGENTS.md §7："no credential is passed as a process argument"）。`bootstrap_admin` 合并进这个 CLI，也支持 login_name。
- 运维重置密码的 CLI（login_name 账号没有邮件找回渠道）。
- 局部移除 local 模式对 SMTP 的依赖：没有 SMTP 时关闭公开注册和找回密码，但预置账号能登录。`/v1/auth/config` 如实告知前端。
- step-up（仅密码）+ `POST /v1/account/password` + `/settings/security` 页面的第一版。
- **前置依赖**：#1030 合并，阶段 0 完成。
- **验收**：`make check`；`make test-db-local`（0107 → 0108 升级时有数据的账号全部保留；在只有 email 账号的库上降级可还原；有 login_name 账号时降级被拒绝）；`make ci-frontend`；运维文档中英双语 + `make docs-verify`；在**没有 SMTP** 的全新部署上走完"预置管理员 → 登录 → 审批 → BYOK → 任务"全链路。

### 阶段 2：邮箱变更、会话管理、已识别设备

- 改绑邮箱（验证新邮箱、通知旧邮箱）、会话列表和撤销、限流的设备豁免（2.2 第二步）。
- 删除账号单独出设计，与 BYOK 的密文销毁 hook 协同。
- **验收**：同上，另加"分布式 50 次失败后，已识别设备仍能登录"的测试。

### 阶段 3：手机号（opt-in）

- `0109_local_otp`、`SmsSender` 接口 + 腾讯实现 + Fake、人机验证接口、手机登录 / 注册一体流程、绑定手机、手机作为 step-up 因子。
- 部署开关：`AGENT_SMS_PROVIDER`、`AGENT_PHONE_LOGIN_ENABLED`、`AGENT_PHONE_PREFIXES`、`AGENT_SMS_DAILY_BUDGET`、人机验证配置。缺任何一项就拒绝启动手机登录。
- 可选：部署方可以设置"申请 Agent 使用前必须绑定已验证手机"，作为反滥用措施（见第 1 节）。
- **前置依赖**：阶段 1 的身份表和阶段 1/2 的 step-up。
- **验收**：`make check` / `make test-db-local` / `make ci-frontend` / `make docs-verify`；Fake 供应商覆盖"注册与未注册号码的响应时间无显著差异"、"每码 5 次后失效"、"冷却与日上限"、"全局预算耗尽后 503"、"错误码也要提交"；**部署方用真实供应商在 staging 完成一次端到端收码**。模拟测试不能替代这一步（规格第 203 行已经写了同样的原则）。

### 阶段 4：同浏览器多账号（按需求决定是否做）

- 浏览器会话表、cookie 值升级、`/v1/auth/accounts|switch`、复用 `SessionSwitchWatcher`、切换时关闭长连接。
- **验收**：除常规 gate 外，测试"成员会话被撤销后不自动切换"、"5 个上限"、"旧 `cals1.` cookie 平滑升级"、"切换后旧终端 WebSocket 被关闭"。

---

## 5. 风险清单

### 安全

1. **手机号可伪造或被夺取**：SIM 换卡、运营商社工、短信劫持（SS7）、恶意软件读取短信。缓解：纯手机号账号由部署方显式允许；建议对管理员账号**禁止**纯手机号（`grant_admin` 要求有密码）；绑定新手机或更换手机时通知已有邮箱。
2. **号码回收 / 换号**：运营商会把注销的号码重新分配（周期从几个月到几年不等【推断】），新持有人用验证码登录就继承了旧账号，包括沙箱 Secrets 和 GitHub 推送能力。缓解：手机号身份设"再验证期限"，例如超过 N 个月未用手机登录时，要求额外因子；在 Profile 里提示用户换号时先解绑；可选接入运营商"二次号"查询【推断，需调研供应商能力】。
3. **OTP 爆破**：见 3.4 的算术。关键是让每码尝试次数绑定发送次数，同时让全局预算有上限。
4. **SMS pumping / 费用攻击**：任何能访问公网的人都能调用，**包括本平台自己的沙箱**（沙箱可访问公网 80/443，`AGENTS.md:229-239`）。缓解：前缀白名单、全局日预算、人机验证、IP 桶，并在 runbook 里写明预算告警。
5. **枚举**：同步调用供应商造成的时序差（上游有这个问题，见 2.5）、"未注册"与"已注册"不同的错误文案、注册时的"号码已占用"。缓解：后台发送 + 登录注册一体 + 统一回答。
6. **授权判断扩展时 fail-open**（见 2.1）：这是**实际存在的回归风险**。阶段 0 必须先修。
7. **登录锁号**（见 2.2）：当前代码中已经存在。
8. **多账号的 login CSRF / 身份混淆**：见 3.3。
9. **凭据进入沙箱**：短信、人机验证的密钥只能存在于 API 进程中，需要测试断言。

### 合规

1. 中国大陆短信要求企业实名、签名报备、模板审核，个人开发者通常无法申请【常识，未查当前法规原文】。所以手机功能**必须是 opt-in**，文档要写清楚"这是部署方的义务"。
2. 手机号属于个人信息（中国《个人信息保护法》、GDPR 都适用）：需要说明收集目的、最小化（限流键哈希、日志掩码、名册不显示）、删除账号时一起删除、导出权。上游明文存储尝试记录的做法不应照搬。
3. 跨境：腾讯云国际站和国内站的数据驻留不同；海外部署者需要另一个供应商，这正是接口 + 单一实现、而不是写死某一家的原因。

### 运维

1. 没有 SMTP 时 local 模式完全不可用（2.3），阶段 1 解决。
2. 反向代理后未开 `AGENT_TRUST_PROXY_HEADERS` 时，`login_ip` 变成全站共享上限（2.2）。runbook 必须写，最好启动时检测。
3. 短信供应商故障时，纯手机号账号**全部无法登录**。缓解：鼓励绑定第二因子（邮箱或密码），并在运维面板展示供应商的失败率。
4. 固定窗口限流在窗口边界允许 2 倍突发，可以接受，但要写进文档。
5. `agent_local_auth_rate_limits` 的写放大：每次登录 3–4 次 upsert；OTP 再加几次。目前每次插入都附带清理（`repositories/accounts.py:591-596`），量大时需要观察。

### 迁移

1. **现有本地库**：local 模式目前只存在于开发、staging 和验收环境（规格第 3–4 行："未合并、部署或迁移生产数据"），但 README 第 53 行表明自托管者可能跟着 `dev` 部署。所以 0108 **必须保留数据**，并经过带数据的升级 / 降级测试，不能假设空库。
2. **降级**：一旦出现非 email 身份，0108 的降级必须拒绝，并在 release note 里说明"0108 之后回滚需要备份恢复"（规格第 154 行已经有同类原则）。
3. **`identity_namespace`**：新表禁用 `user_id` / `target_user_id` 列名（3.1 第 5 点），否则会改变"空库"判断，影响 A3 迁移和首次绑定。
4. **迁移编号**：#1030 已经因为与 #1029 并行而把编号改成 0107（`0107:10-11`）。后续阶段在合并时统一分配编号，避免和 `dev` 上其他迁移抢 head（`dev` 当前最新是 `0105_job_event_usage_summary`）。
5. **未来生产库（Gateway → local，A3）**：身份表应预留 `gateway_legacy` 身份类型的可能（3.1 第 6 点），否则 A3 又要做一次 schema 演进。

---

## 6. 给产品负责人的一段话

我同意"身份模型应该支持多种登录方式"，也同意现在的实现把 email 写得太死。规格本来就规划了"登录类型"，实现没有照做，这一点应该纠正。

但我不同意把手机号登录、多账号放进首版或 #1030。一是本产品用户都有邮箱（他们要连 GitHub）；二是开源自托管者大多拿不到短信资质；三是当前代码里有**两个更紧急的真问题**：任何人用一个 IP 就能锁死别人的账号，以及没有 SMTP 的部署根本无法使用 local 模式。

建议的顺序是：先修这两个问题，再用一个小迁移把身份抽象出来，同时提供无邮箱的用户名账号和自助改密码；手机号作为部署方可选开启的功能放在第三阶段，并把它首先当作反滥用的实名信号，而不只是一种登录方式。

---

## 附录 A：Codex 的独立复核与修正（2026-10-29）

本节由 Codex 在拿到上面这份评审后独立核对加入，不是 Claude 的原文。凡与上文冲突，以本节为准。

### A.1 复核通过的论断

下面每条都在本机 `origin/pr1030` 上重新读过，结论成立：

| 上文位置 | 论断 | 复核结果 |
|---|---|---|
| §2.2 | 登录限流对每次尝试无条件计数，单 IP 可锁死账号 | **成立**。`api/auth.py:112-119` 三个桶，`_local_auth.py:297-306` 循环内不短路，`storage/repositories/accounts.py:570-599` 的 `INSERT ... ON CONFLICT DO UPDATE SET hits = hits + 1` 无条件下计数；且 `enforce_rate_limits` 在 `flows.sign_in` 之前调用。被拒绝的请求同样计数。 |
| §2.2 | `tests/` 里没有这些桶的用例 | **成立**。`git grep login_email -- tests/` 无结果；只有 `test_local_account_store.py:546-556` 直接测 `hit_rate_limit` 本身。 |
| §2.3 | 没有 SMTP 时 local 模式完全不可用 | **成立**，且比上文更硬。`_local_auth.py:243-250` 的 `signup_enabled` 要求 `mailer.configured` 与 Web base；`bootstrap_admin.py:51-52` 要求账号**已**验证邮箱；`git grep INSERT INTO agent_local_accounts` 只有 `repositories/accounts.py:216` 一处，即只有 signup 能建号。**引导链条闭合：无 SMTP → 无法建号 → 无法验证 → 无法登录，也建不出第一个管理员。** |
| §2.1 | `local_access_ended()` 是潜在 fail-open | **成立**（今天不可触发）。`_sql/accounts.py:18-25` 用 INNER JOIN `agent_local_password_logins` 后取 `EXISTS`；`_create_account`（`repositories/accounts.py:206-237`）在同一事务内同时插三张表，所以今天每个账号必有该行。 |
| §2.1 | 该栅栏在 residency 与模型连接上保护撤权 | **成立**。`storage/repositories/residency.py:25,145` 与 `model_connections.py:401` 都在 SQL 条件里用 `NOT local_access_ended(...)`。 |
| §2.1 | `_ACCOUNT_FROM` 是 INNER JOIN，缺行则读不到账号 | **成立**。`repositories/accounts.py:50-54`。 |
| §1 | 规格第 117 行原本规划了"登录类型" | **成立**。原文："登录身份：账号 ID、**登录类型**、唯一规范化标识、验证状态；密码只存哈希。" |
| §1 | README 声明 self-hosted 且无版本化 release | **成立**。`README.md:9` "Self-hosted coding agents…"；`README.md:52-53` "there are no versioned releases yet, and configuration and APIs can change between commits"。 |
| 头部 | 派发时 Claude 的 effort 未单独指定 | **成立，是 Codex 的疏漏**。启动命令用了 `--effort high`，但 prompt 里没写明。文档头部那句"委托人未单独指定"的表述应读作"prompt 未声明"，实际按 high 运行（与 `~/.claude/settings.json` 的 `effortLevel: high` 一致）。 |

### A.2 需要修正的地方

1. **§3.1 第 2 点的 ULID 建议自相矛盾，应当废弃。** 上文 DDL 写的是
   `id TEXT PRIMARY KEY CHECK (id ~ '^[0-9A-HJKMNP-TV-Z]{26}$')`（ULID，与
   `0107_local_accounts.py:35` 的 `agent_local_accounts.id` 一致），紧接着却建议
   "改为 `TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text`"。UUID 不满足那条 26 位
   Crockford base32 的 CHECK，两条建议不能并存。
   **正确做法**：新表继续用 ULID，与 `agent_local_accounts` 保持同一种 id 形态；
   迁移里的 id 由 Python 侧生成后作为参数传入（`tokens.new_account_id()` 已有实现），
   不在 SQL 里拼 ULID。若想简化，用 `(kind, value_normalized)` 作主键也是可行替代，
   但那就**不能**再保留 ULID 的 CHECK。

2. **§2.2 的"键规范化差异"未证实，应降级为待验证。** 上文自己也标注了未运行验证。
   实读代码：`_email_key` 是 `strip().lower()`（`api/auth.py:61-64`），
   `normalize_email` 返回 `validate_email(...).normalized` 再 `.lower()`
   （`application/accounts.py:82-93`）。两者对 IDN / Unicode 归一化的差异是**推断**，
   未构造用例验证。不应当作已确认缺陷写进结论；建议列为一个待补的单元测试，而不是
   一个已定性的安全问题。

3. **§3.1 建议的 `agent_local_account_access` 视图需要一处补强。** 视图用
   `EXISTS (... verified_at IS NOT NULL)` 判 `may_sign_in`，这在"账号一个身份都没有"
   时返回假，方向正确（fail-closed）。但视图是 `JOIN agent_local_admissions`，
   **INNER JOIN**：若某个账号缺 admissions 行，该账号在视图里不存在，于是
   `local_access_ended` 里 `NOT x.has_use` 找不到行 → `EXISTS` 为假 → 又变成 fail-open。
   今天 `_create_account` 保证三行同建，但既然目标是消除这类隐式前提，视图应当写成
   `LEFT JOIN` 并在 `may_sign_in`/`has_use` 里用 `coalesce(..., false)`。这条是本附录
   新增的，上文的方案里没有。

4. **阶段 0 的门槛表述。** 上文说前两项"是合并的必要条件"。我同意限流缺陷是必须修的
   （它是当前代码里真实存在的、可被单 IP 触发的账号锁死）。但第二项（收敛 `email_verified`
   的 7 处判断）只是**为将来扩展做准备的纯重构**，它修的是今天触发不到的 fail-open。
   把它设为 #1030 的合并门槛会把一个 43k 行的 PR 再往后拖；更合适的做法是放进阶段 1
   的那个 PR 里，和 schema 变更一起做，因为那时才真正需要它。

### A.3 Codex 的结论

与上文的实质结论一致，但优先级排序我调整如下：

1. **修登录限流**（单 IP 锁号）——这是唯一我认为必须进 #1030 的。
2. **身份抽象 + login_name 账号 + 无 SMTP 可引导**——`#1030` 之后的第一个 PR。这条我
   比上文更强调：一个自托管产品如果"没有 SMTP 就连第一个管理员都建不出来"，那不是
   一个身份模型的范围问题，是**产品不可用**问题。
3. **自助改密码 / step-up**——和 2 一起。
4. **手机号**——晚做，opt-in，且优先当作准入的反滥用信号而不是主要登录方式。
5. **多账号同浏览器**——最低优先级。

换句话说：**产品负责人"应该支持这些功能"的方向我同意，但不同意"因此这个 design 就不对"的
全盘否定**。这份 design 真正的错不在"没做手机号"，而在两处更基础的地方：它把 email 写进了
授权判断（规格本来规划的是可扩展的"登录类型"），以及它让 local 模式的可用性依赖 SMTP。
手机号是这两处之后的自然延伸，不是替代。
