# 本地账号的登录身份：多身份、无邮箱引导与敏感操作确认

日期：2026-10-29。状态：设计已确认方向，**未实现**。本文取代
[独立账号与 waiting list 设计](2026-10-27-independent-accounts-and-waitlist-design.zh-CN.md)
第 22 行「手机号登录、多账号切换不包含在此版」的范围决定，并把「首版只支持邮箱」
改为「账号可以有多种登录身份，按其拥有的身份证明身份」。

评估依据：[身份范围评审](../reviews/2026-10-29-identity-scope-review.zh-CN.md)。

## 1. 为什么要改

现有实现（`AGENT_AUTH_MODE=local`，PR #1030）把「账号」和「邮箱密码凭据」写成了一对一，
并把邮箱验证状态直接写进了授权判断。这带来三个问题，前两个是缺陷，第三个是范围偏差。

### 1.1 没有 SMTP 的部署完全无法使用（缺陷）

引导链条是闭合的：

1. `signup_enabled()` 要求邮件投递已配置并有一个合法的 Web base URL
   （`api/_local_auth.py:243-250`）。
2. `bootstrap_admin` 要求账号**已经**验证过邮箱（`accounts/bootstrap_admin.py:51-52`）。
3. `INSERT INTO agent_local_accounts` 在整个后端只有一处
   （`storage/repositories/accounts.py:216`），即只有公开注册能建号。
4. 邮箱只能通过邮件里的链接验证。

所以内网、家庭实验室、离线评估这类**没有可用 SMTP** 的部署，既注册不了账号，也建不出第一个
管理员。对一个 MIT 许可的自托管产品（`README.md:9`）来说，这不是范围问题，是产品不可用。

### 1.2 撤权栅栏在扩展身份后会 fail-open（潜在缺陷）

`storage/_sql/accounts.py:18-25` 的 `local_access_ended()` 用 `EXISTS (... INNER JOIN
agent_local_password_logins ...)` 判断「这个 owner 的访问已结束」。它保护的正是
`storage/repositories/residency.py:25,145` 和 `model_connections.py:401` 上的撤权路径。

今天结果正确，因为 `_create_account`（`storage/repositories/accounts.py:206-237`）在同一
事务里同时插入账号、凭据、申请三行。但只要出现**没有凭据行**的账号（纯手机号、纯
login_name），JOIN 就没有结果，`EXISTS` 为假，「访问未结束」成立 —— 一个已被撤销或停用的
账号会**继续通过**栅栏。这是扩身份之前必须先消除的隐式前提。

### 1.3 规格本来规划的是可扩展身份（范围偏差）

第 117 行原文：

> 登录身份：账号 ID、**登录类型**、唯一规范化标识、验证状态；密码只存哈希。

实现把「登录类型」折叠成了「email」。`email_verified` 因此硬编码进了至少七处授权判断
（评审记录 §2.1 有完整清单）。修这个偏差，比继续在 email 上打补丁便宜。

## 2. 已确认的产品方向

- **账号是 owner；身份是「如何证明你是这个 owner」；密码是身份的一种凭据。** 三者分开，
  符合规格原本的意图，也为 A3（Gateway → 本地身份迁移）留出位置。
- **多身份，但首版每类最多一个。** 一个账号可以同时有邮箱、手机号、login_name，
  但每种一个；「绑定第二个邮箱」属于后续阶段。
- **每类身份都必须可以选择不存在。** 运维预置的账号可以只有 login_name，纯手机号账号
  可以没有邮箱也没有密码。
- **手机号默认关闭，且是部署方显式开启的能力**，不是产品默认路径。
- **敏感操作确认（step-up）不绑死在手机 OTP 上**：先做「重新输入密码」，
  手机 OTP 以后作为可用因子之一。

### 2.1 各能力的分期结论

| 能力 | 结论 | 主要理由 |
|---|---|---|
| 运维预置 `login_name` 账号 | **第 1 阶段** | 这是解开 §1.1 死锁的最小改动，不引入任何外部依赖 |
| 多身份数据模型（`0108`） | **第 1 阶段** | 消除 §1.2 的 fail-open，并让后续能力有地方落 |
| 自助改密码 + step-up 框架 | **第 1 阶段** | 现在完全没有改密码入口；运维不该是唯一途径 |
| 改绑邮箱、会话管理 | 第 2 阶段 | 依赖 step-up |
| 已识别设备豁免（限流） | 第 2 阶段 | 独立的小改动，解决分布式锁号 |
| 手机号 + 验证码登录 | 第 3 阶段（opt-in） | 部署侧合规依赖；且是**较弱**的单一因子，见 §7.1 |
| 同浏览器多账号 | 第 4 阶段 | Cloud Agent 已有「单会话 + 切换」的决定（#685），收益低于代价 |

手机号首先是**准入时的反滥用信号**（中国大陆的号码是实名的，伪造成本远高于邮箱），
其次才是登录方式。部署方可以要求「申请前必须绑定已验证手机」。

## 3. 数据模型：`0108_local_login_identities`

### 3.1 表

```sql
-- 登录身份：一类一行。格式约束落在数据库上，不依赖调用方记得检查。
CREATE TABLE agent_local_login_identities (
    id TEXT PRIMARY KEY CHECK (id ~ '^[0-9A-HJKMNP-TV-Z]{26}$'),
    account_id TEXT NOT NULL REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('email', 'phone', 'login_name')),
    value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 320),
    value_normalized TEXT NOT NULL,
    verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (kind, value_normalized),
    UNIQUE (account_id, kind),
    CHECK (
        (kind = 'email'      AND position('@' IN value_normalized) > 1
                             AND value_normalized = lower(value_normalized))
     OR (kind = 'phone'      AND value_normalized ~ '^\+[1-9][0-9]{7,14}$')
     OR (kind = 'login_name' AND value_normalized ~ '^[a-z][a-z0-9._-]{2,31}$')
    )
);
CREATE INDEX idx_agent_local_login_identities_account
    ON agent_local_login_identities (account_id);

-- 密码凭据：可选。没有这行的账号只能用 OTP 或运维方式进入。
CREATE TABLE agent_local_password_credentials (
    account_id TEXT PRIMARY KEY REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
    password_hash TEXT NOT NULL CHECK (left(password_hash, 10) = '$argon2id$'),
    password_changed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

三点说明：

1. **`id` 继续用 ULID**，与 `agent_local_accounts.id`
   （`0107_local_accounts.py:35`）同形。ULID 由 Python 侧生成后作为参数传入
   （`accounts/tokens.py` 的 `new_account_id()` 已有实现），**不在 SQL 里拼**。
   （评审记录 §3.1 曾建议改用 `gen_random_uuid()::text`，与那条 26 位 Crockford base32
   的 CHECK 冲突，已在该文附录 A.2 纠正。）
2. **格式互斥写进 CHECK**：email 必含 `@`，phone 必是 E.164，login_name 必以字母开头且
   不含 `@`/`+`。上游「login_name 不含 `@`」只是注释里的约定；这里成为约束，
   使限流键即使忘了加类型前缀，不同类型也不会撞。仍然要加前缀（§4），两道防线。
3. **不使用 `user_id` / `target_user_id` 列名。** `identity_namespace._has_owner_data`
   （`identity_namespace.py:25-37`）把带这两列、以 `agent_` 开头的表当作 owner 数据；
   `0107` 的文档字符串因此刻意避开了它们。新表一律用 `account_id`，并新增契约测试
   断言 `agent_local_%` 表不含这两个列名。

### 3.2 迁移

`upgrade()` 在一个事务里：

1. 建两张新表。
2. 把 `agent_local_password_logins` 的每一行拆成一条 `email` 身份 + 一条密码凭据。
   身份的 `verified_at` 取 `email_verified_at`。
3. `DROP TABLE agent_local_password_logins`。

`downgrade()` 只有在每个账号都恰好有「一条 email 身份 + 一份密码」时才还原
（重建 `agent_local_password_logins`）；否则 `RAISE`，拒绝降级。不能静默丢掉
一个 login_name 或手机号账号。

**必须保留数据**：`README.md:52-53` 说明本项目尚无版本化 release、配置与 API 可随提交变化，
自托管者可能直接跟 `dev`。所以要经过带数据的升级/降级测试，不能假设空库。

### 3.3 授权判断收敛为一处

```sql
CREATE VIEW agent_local_account_access AS
SELECT a.id AS account_id,
       (a.status = 'active'
        AND EXISTS (SELECT 1 FROM agent_local_login_identities i
                    WHERE i.account_id = a.id AND i.verified_at IS NOT NULL)) AS may_sign_in,
       (a.status = 'active'
        AND coalesce(m.status = 'approved', false)
        AND EXISTS (SELECT 1 FROM agent_local_login_identities i
                    WHERE i.account_id = a.id AND i.verified_at IS NOT NULL)) AS has_use
FROM agent_local_accounts a
LEFT JOIN agent_local_admissions m ON m.account_id = a.id;
```

- **`LEFT JOIN` 是必须的。** 用 INNER JOIN 时，缺 `admissions` 行的账号在视图里不存在，
  于是 `local_access_ended` 里的 `NOT x.has_use` 找不到行 → `EXISTS` 为假 → 又变成
  fail-open。`coalesce(..., false)` 兜住这一层。
- `local_access_ended(col)` 改写为
  `EXISTS (SELECT 1 FROM agent_local_account_access x WHERE x.account_id = col AND NOT x.has_use)`。
  Gateway 模式下没有本地账号，结果恒为假，行为不变。
- 缺任何一行时 `has_use` 为假，**fail-closed**。
- `permissions_for` / `may_sign_in` 只看 `identity_verified`，Python 与 SQL 的规则
  由一份契约测试互相核对，防止两边漂移。

`AccountRecord` 随之变化：`email` 改为 `str | None`，新增 `login_name`、`phone_masked`、
`identity_verified`。`user_email()` 改为 `user_display()`，按 email → login_name 取，
**不返回手机号**：团队名册不应暴露成员手机号，Profile 只显示自己的掩码号码。

## 4. 登录标识与限流分桶

### 4.1 标识解析

```python
@dataclass(frozen=True)
class LoginIdentifier:
    kind: Literal["email", "login_name", "phone", "raw"]
    normalized: str

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.normalized}"   # 类型前缀 + 规范化值
```

- **密码登录**只接受 `email` 与 `login_name`。字段改名 `identifier`，API 保留 `email`
  作为兼容别名。**手机号不参与密码登录**：手机号账号首版无密码，只走 OTP 端点。
  这样密码路径与 OTP 路径的限流、枚举防护完全分开。
- 无法解析的输入退到 `raw:` 键。否则畸形输入可以绕过 per-identifier 桶，只消耗 per-IP 桶。
- 限流键经 `tokens.rate_limit_key(bucket, ident.key)`，它已经把 bucket 名混进 HMAC
  （`accounts/tokens.py:64-66`）；类型前缀再让同一桶内不同类型不可能冲突。正确性由
  **构造**保证，不依赖「login_name 不含 `@`」这种格式巧合。
- 账号不存在时保留现有的假密码校验（`application/accounts.py:160-167`），
  login_name 查不到时同样执行。

### 4.2 限流分桶（阶段 0 已修）

登录限流必须先过 per-address 桶，只有通过的尝试才计入 account-wide 桶。详见
[评审记录](../reviews/2026-10-29-identity-scope-review.zh-CN.md) §2.2：当前实现里
**单个 IP 在 15 分钟内发 50 次请求就能让账号所有者在任何 IP 都登不进去**，
且被拒绝的请求同样计数。

修正后的语义：

| 桶 | 键 | 窗口 / 上限 | 计数规则 |
|---|---|---|---|
| `login_ident_ip` | 标识 + 地址 | 15 min / 5 | 每次尝试 |
| `login_ip` | 地址 | 1 h / 20 | 每次尝试 |
| `login_ident` | 标识 | 15 min / 50 | **只计通过上面两个桶的尝试** |

单个地址最多给 account 桶贡献 5 次，所以锁死账号需要 ≥10 个不同地址。
彻底的分布式锁号防护是阶段 2 的「已识别设备豁免」：登录过的浏览器拿到一个
`__Host-` HttpOnly 设备凭据（服务端只存 keyed hash），此后该浏览器的尝试只记入自己的
设备桶，不受他人尝试影响。

**部署前提**：`get_client_ip`（`api/deps.py:749-774`）只有在 `AGENT_TRUST_PROXY_HEADERS`
打开时才读取 `X-Forwarded-For`。反向代理后未开启时，所有请求共用代理地址，
`login_ip` 的 20 次/小时会变成**整个部署的登录上限**。运维文档必须写明，
并在启动时检测「所有请求都来自同一私网地址」时告警。

## 5. 引导：没有 SMTP 的部署

运维 CLI（不经过 HTTP，因此不需要邮件）：

```bash
# 密码从 TTY 或 stdin 读取，绝不经过 argv（AGENTS.md §7）
AGENT_AUTH_MODE=local python -m cloud_agent.accounts.provision \
    --login-name alice --email alice@example.org --approve --admin
```

- 它合并现有 `bootstrap_admin` 的能力：能建号、能授予管理员、能批准申请。
- 密码从 TTY/stdin 读，**不走命令行参数**（`AGENTS.md` §7：「no credential is passed
  as a process argument」）。
- 同时提供运维重置密码的 CLI：login_name 账号没有邮件找回渠道。
- 没有 SMTP 时：关闭公开注册与找回密码，但**预置账号仍能登录**。
  `/v1/auth/config` 如实告诉前端哪些方式可用。

这使「无 SMTP」从「完全不可用」变成「用运维 CLI 开一个号，其余照常」。

## 6. 敏感操作确认（step-up）

```
POST /v1/auth/reauth {method:"password", password}        → 会话上置 5 分钟窗口
POST /v1/auth/reauth {method:"otp", challenge_id, code}   → 同上（阶段 3 之后）

POST /v1/account/password {new_password}   需要 step-up；撤销其他会话
POST /v1/account/email    {new_email}      需要 step-up；验证新邮箱；通知旧邮箱
POST /v1/account/phone    {challenge_id, code}  需要 step-up；绑定已验证号码
DELETE /v1/account                          需要 step-up；走 terminate_owner_access
```

- step-up 标记存在**会话行**上（`agent_local_sessions.reauth_at`），不另发 cookie。
- 可用因子取决于账号拥有什么：有密码用密码；只有手机用发到**本账号已验证号码**的 OTP。
- **删除账号单独设计**，不放进这一批：规格 §4 要求撤销模型连接、销毁密文、停止驻留进程，
  与 `agent_local_access_terminations` 的终止流程同复杂度。

## 7. 手机号（阶段 3，opt-in）

### 7.1 它是较弱因子，必须这样对待

Cloud Agent 账号控制沙箱，沙箱里有 owner 的 Secrets、GitHub App 推送 token、已登记的
SSH 服务器（`AGENTS.md` §7）。只用一个短信验证码作为**唯一因子**，会让 SIM 换卡、
号码回收、短信劫持直接变成账号接管。

因此：

- 纯手机号账号必须由部署方**显式允许**；**管理员账号禁止纯手机号**
  （`grant_admin` 要求存在密码凭据）。
- 手机号的主要价值是**实名反滥用信号**，用于准入审批，而不是主登录方式。
- 绑定新手机或换号时通知已有邮箱。

### 7.2 存储

```sql
CREATE TABLE agent_local_otp_challenges (
    id TEXT PRIMARY KEY,                                   -- 返回前端的 challenge_id
    purpose TEXT NOT NULL CHECK (purpose IN ('phone_login','phone_bind','step_up')),
    channel TEXT NOT NULL CHECK (channel IN ('sms','email')),
    destination_hash BYTEA NOT NULL CHECK (length(destination_hash) = 32),  -- keyed hash
    account_id TEXT REFERENCES agent_local_accounts (id) ON DELETE CASCADE,
    code_hash BYTEA NOT NULL CHECK (length(code_hash) = 32),
    state TEXT NOT NULL CHECK (state IN ('reserved','sent','consumed','failed')),
    attempts SMALLINT NOT NULL DEFAULT 0,
    max_attempts SMALLINT NOT NULL DEFAULT 5,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX agent_local_otp_open ON agent_local_otp_challenges
    (purpose, destination_hash, coalesce(account_id, '')) WHERE state IN ('reserved','sent');
```

- **不存明文号码**，只存 keyed hash。上游 `serving/storage/phone_auth.py:86-93` 的
  `phone_auth_attempts` 存明文号码，与本仓库「限流键只存 keyed hash」的约定冲突，不照抄。
- `code_hash = HMAC(derived_key("otp"), "otp-v1\0" ‖ challenge_id ‖ purpose ‖
  destination ‖ account_id ‖ code)`，密钥用现有 `derived_key` 的 HKDF 用途隔离
  （`accounts/config.py:95-111`），**不借用会话密钥**。
- **限流复用现有 `agent_local_auth_rate_limits`**（Postgres 固定窗口，多副本共享），
  不另建 attempts 表，也不需要上游那种 `pg_advisory_xact_lock` + 明文号码的计数。
  桶：发送冷却 60s/1、每号 1h/5 与 1d/10、每地址 1h/10 与 1d/30、
  每国家码 1d（部署配置）、**全局 1d（部署配置，费用上限）**；
  校验每号 15min/10、每地址 1h/30。

### 7.3 爆破算术（关键）

6 位码、每码最多 5 次尝试，且**每次尝试都绑定在某一次发送上**：

- 每号每天最多 10 次发送 × 5 次 = 50 次猜测 → 日成功率约 5×10⁻⁵，**年约 1.8%**。
- 若让 verify 与 send 解耦（只按 15 分钟 10 次算）→ 年成功率约 **35%**。

所以 **challenge 的 `attempts` 必须是主约束**，verify 桶只作辅助。消费在同一事务内
`SELECT ... FOR UPDATE` 后 `attempts + 1` 并 `compare_digest`，成功才 `state='consumed'`；
失败尝试也要提交。**先预留（`reserved`）后发送**，供应商确认才置 `sent`，失败置 `failed`，
这样「发出去了但没记录」的码不可能被使用。

### 7.4 防枚举与防短信轰炸

- **登录/注册一体**：任何号码在配额内都会收到码，响应**立即**返回，短信在
  `BackgroundTasks` 里发（与现有邮件同样的做法）。响应时间与号码是否已注册无关。
  验证通过后：有账号就登录；没有且部署开放注册就建号，否则显示「该部署未开放注册」。
- **SMS pumping**：任何能访问公网的人都能调用，**包括本平台自己的沙箱**
  （沙箱可访问公网 80/443）。所以开启手机登录必须三项齐全，缺一项拒绝启动：
  ① 号码前缀白名单（部署方声明允许的国家码）、② 全局日预算、③ 人机验证。
- 人机验证只做接口 + 一个实现；不硬编码 Turnstile（其在中国大陆可达性不稳定）。

### 7.5 短信 provider：一个接口 + 一个实现

仿现有 `AccountMailer`（`accounts/mail.py`，`configured` + `send`）：

```python
class SmsSender(Protocol):
    @property
    def configured(self) -> bool: ...
    async def send_code(self, e164: str, code: str, *, locale: str) -> bool: ...

def sms_sender_from_env() -> SmsSender: ...   # disabled | tencent
```

不引入上游的 `delivery` provider 注册表：Cloud Agent 只有 SMTP 一种投递，为第二种建
注册表属于过度设计。**不做通用 webhook 实现**：它会把验证码明文送到任意 URL。

短信供应商的 SecretId/SecretKey 属于平台长期凭据，只能存在于 API 进程环境中；
需要测试断言它们不出现在 runner、host claim、沙箱的环境变量白名单里。供应商异常信息
可能带凭据或验证码，只记录错误类型，不记录消息。

## 8. 前端

`GET /v1/auth/config` 扩展为：

```json
{
  "mode": "local",
  "signup_enabled": true,
  "methods": {
    "password": { "identifiers": ["email", "login_name"] },
    "phone_otp": { "enabled": false, "signup": false, "prefixes": ["+86"] }
  },
  "multi_account": false
}
```

- `LoginPageClient.tsx` 的结构不变：`gateway` → `GatewayLoginPage`，`local` →
  `LocalLogin`。`LocalLogin` 按 `methods` 渲染：只有密码时就是今天的表单，
  输入框标签改为「邮箱或用户名」；`phone_otp.enabled` 时增加页签。
  **未开启的方式完全不渲染**，不留灰色占位。
- 手机组件只做国家码选择（只列 `prefixes` 声明的）、号码输入、发送冷却倒计时、
  6 位码输入。**不搬上游 260 行的全量国家表**。
- 新增 `/settings/security`：改密码、绑定/更换邮箱、绑定手机、会话列表（撤销其他会话）。

## 9. 会话与多账号（阶段 4）

不建议用 cookie 数组：每个元素都是长期凭据，`__Host-` 的单值语义、体积、按条撤销都会变复杂。

建议「浏览器会话」：

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
    ADD COLUMN browser_session_id TEXT
        REFERENCES agent_local_browser_sessions (id) ON DELETE CASCADE;
```

- cookie 仍只有一个，属性完全不变（`__Host-cloud_agent_local_session`、HttpOnly、
  SameSite=Lax、Path=/）；值改成浏览器会话令牌，用新前缀（如 `calb1.`）区分，
  旧 `cals1.` 值第一次出现时就地升级并作废旧值。
- 每个成员会话仍是 30 天固定、不续期、可单独撤销。加入新账号**不延长**已有成员的期限。
- **成员会话被撤销后不自动切到另一个账号**，返回 401 + `choose_account`，
  由前端显示选择器。自动切换会让用户在不知情时以另一个身份操作。
- 切换时已打开的终端 WebSocket / SSE **不迁移**：关闭它们，并复用现有
  `SessionSwitchWatcher` 把所有标签页带回首页。
- 端点：`GET /v1/auth/accounts`（只返回 id、显示名、掩码标识，**不返回任何令牌**）、
  `POST /v1/auth/switch`、`POST /v1/auth/logout {scope}`，
  全部走现有跨站写入保护与 `require_json`。
- 「登录 CSRF」在多账号下更隐蔽（受害者原账号还在列表里），所以 `signup`/`login`
  继续强制跨站写入检查，切换器要醒目显示当前账号。

**过渡方案**（阶段 1 即可有）：local 模式的「切换账号」= 退出 + 跳
`/login?login_hint=<掩码标识>`。后端已支持（`api/auth.py:122-125` 登录时撤销旧会话），
前端改动很小。

## 10. 分期与验收

每期都在同一个 PR 内附对应文档更新（本仓库要求设计与实现同 PR）。

### 阶段 0 —— 进 #1030

| 改动 | 验收 |
|---|---|
| 登录限流：先过 per-address 桶，只有通过的才进 account 桶（§4.2） | 「单 IP 打 60 次后，账号主人从另一 IP 用正确密码能登录」；「≥10 个 IP 各贡献 5 次后 account 桶才触发」；`make test-db-local` |

限流是当前代码里真实存在、可被单个 IP 触发的账号锁死，**必须在合并前修**。

### 阶段 1 —— #1030 合并后的第一个 PR

`0108_local_login_identities` 数据迁移 + 授权视图；`provision` CLI（含 login_name、
管理员、密码重置）；无 SMTP 时「关公开注册但预置账号可登录」；step-up（仅密码）
+ `POST /v1/account/password` + `/settings/security` 首版；收敛 §1.3 的七处
`email_verified` 判断。

验收：`make check`；`make test-db-local`（0107→0108 带数据升级、所有账号保留；
纯 email 库可降级还原；存在 login_name 账号时降级被拒绝）；
`make ci-frontend`；`make docs-verify`；**在无 SMTP 的全新部署上**走完
「预置管理员 → 登录 → 审批 → BYOK → 任务」全链路。

### 阶段 2 —— 改绑邮箱、会话管理、已识别设备

验收加：「分布式 50 次失败后，已识别设备仍能登录」。

### 阶段 3 —— 手机号（opt-in）

`0109_local_otp`、`SmsSender` + 一个实现 + Fake、人机验证接口、登录/注册一体、
绑定手机、手机作为 step-up 因子；`AGENT_SMS_PROVIDER`、`AGENT_PHONE_LOGIN_ENABLED`、
`AGENT_PHONE_PREFIXES`、`AGENT_SMS_DAILY_BUDGET`、人机验证配置，缺任一项拒绝启动。

验收：Fake 覆盖「注册与未注册号码响应时间无显著差异」「每码 5 次后失效」
「冷却与日上限」「全局预算耗尽后 503」「错误码也要提交」；**部署方用真实供应商在
staging 完成一次端到端收码** —— 模拟测试不能替代这一步。

### 阶段 4 —— 同浏览器多账号（按需求决定是否做）

验收加：「成员会话被撤销后不自动切换」「上限 5 个」「旧 `cals1.` cookie 平滑升级」
「切换后旧终端 WebSocket 被关闭」。

## 11. 风险

### 安全

1. **手机号可伪造或被夺取**：SIM 换卡、运营商社工、短信劫持、恶意软件读短信。
   缓解见 §7.1：管理员禁用纯手机号、纯手机号账号需部署方显式允许。
2. **号码回收/换号**：运营商会重新分配注销号码，新持有人用验证码登录就继承旧账号，
   包括沙箱 Secrets 与 GitHub 推送能力。缓解：给手机身份设「再验证期限」
   （如超过 N 个月未用手机登录即要求额外因子）；Profile 提示换号先解绑。
3. **OTP 爆破**：见 §7.3 的算术；关键是每码尝试次数绑定发送次数 + 全局预算上限。
4. **SMS pumping / 费用攻击**：见 §7.4；沙箱本身就能访问公网，所以预算告警要进 runbook。
5. **枚举**：同步调用供应商会造成时序差（上游有此问题）；必须后台发送 + 登录注册一体 +
   统一回答。
6. **扩身份时 fail-open**：§1.2 的栅栏，阶段 1 必须先修。这是**实际的回归风险**。
7. **登录锁号**：§4.2，当前代码已存在，阶段 0 修。
8. **多账号的 login CSRF / 身份混淆**：见 §9。
9. **凭据进沙箱**：短信与人机验证密钥只能存在于 API 进程，需测试断言。

### 合规

1. 中国大陆短信需企业实名、签名报备、模板审核，个人开发者通常无法申请；所以必须 opt-in，
   文档写明「这是部署方的义务」。
2. 手机号是个人信息（《个人信息保护法》、GDPR 均适用）：需说明收集目的、
   最小化（限流键哈希、日志掩码、名册不显示）、删除账号时一并删除。
3. 跨境：供应商国内站与国际站的数据驻留不同，所以做「接口 + 单一实现」而非写死一家。

### 运维

1. 无 SMTP 时 local 模式不可用（阶段 1 解决）。
2. 反向代理后未开 `AGENT_TRUST_PROXY_HEADERS` 会让 `login_ip` 变成全站共享上限。
3. 短信供应商故障时纯手机号账号全部无法登录；应鼓励绑定第二因子。
4. 固定窗口限流在窗口边界允许 2 倍突发，可接受但要写进文档。
5. `agent_local_auth_rate_limits` 的写放大：每次登录 3–4 次 upsert，OTP 再加几次；
   目前每次插入都附带清理，量大时需观察。

### 迁移

1. 现有本地库：local 模式目前只存在于开发/验收环境，但自托管者可能跟 `dev`，
   所以 `0108` 必须保留数据并经带数据升级/降级测试。
2. 降级：出现非 email 身份后必须拒绝，release note 说明「0108 之后回滚需备份恢复」。
3. `identity_namespace`：新表禁用 `user_id`/`target_user_id` 列名，否则会改变「空库」判断，
   影响 A3 迁移与首次绑定。
4. 迁移编号在合并时统一分配，避免与其他分支抢 head（`dev` 当前是
   `0105_job_event_usage_summary`，本 PR 已用 `0106`/`0107`）。
5. 未来 Gateway → local 迁移（A3）：身份表应预留 `gateway_legacy` 类型的可能
   （`value_normalized = issuer ‖ '\x00' ‖ subject`），只用于证明「本地账号继承了该 owner」，
   **不能用来登录**。

## 12. 与既有设计的关系

- [独立账号与 waiting list 设计](2026-10-27-independent-accounts-and-waitlist-design.zh-CN.md)
  第 22 行的范围决定由本文取代；该文的其余部分（准入、会话、撤权、复用来源）不变。
- 复用 HybridInference 的边界不变：仍是**拷贝 + 固定 commit**，不是 import
  （`tests/unit/test_scaffold.py::test_no_gateway_imports` 强制）。本文新增的部分
  （身份表、OTP、浏览器会话、step-up）是 Cloud Agent 自己的实现，
  只借鉴上游的**安全语义**，不复用其代码结构；特别是不照抄其明文号码的尝试表。
