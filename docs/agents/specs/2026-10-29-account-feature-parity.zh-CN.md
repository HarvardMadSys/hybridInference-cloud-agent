# 本地账号与 HybridInference 账号功能对齐（feature parity）

- **日期**：2026-10-29。
- **状态**：**定稿**，对应交付提交（见任务交付文档 `delivery.md` 中的最终 SHA）。下表每一行都标出了实现位置、前端入口和测试，状态以该提交为准；最终门禁与真实浏览器 smoke 的结果也记录在交付文档中。
- **比较源**：HybridInference `origin/dev` `8b49872d1dcbed5c4bc8132b026b6740c3437aa7`，只读。原始 copy 来源是 `321e9a6c`。
- **Cloud Agent 起点**：PR #1030 head `506f33fe` 合入 dev `4b7b826d`。

本文取代 [本地登录身份设计](2026-10-29-local-login-identities-design.zh-CN.md) 和[身份范围评审](../reviews/2026-10-29-identity-scope-review.zh-CN.md)里的**分期与排除决定**。那两份文件中经核实成立的证据和设计继续沿用：身份表加 `LEFT JOIN` 访问视图、限流分桶。

独立评审的发现及其处理（B1–B20、N1–N10、R1–R3、F1–F11）不在本文逐条展开，见任务证据目录中的 resolution map。

## 一、范围（用户确认）

**在范围内：HybridInference 账号身份与管理的全部能力，复制或改编到本地实现。**

- **注册与验证**：注册、验证、找回与修改密码、ToS、captcha、注册策略。
- **登录**：邮箱、登录名、手机号三种方式。
- **身份绑定**：邮箱与手机号绑定。
- **账号生命周期**：首次部署建管理员；删号，包括自助与管理员两种，以及软删、恢复、彻底擦除。
- **会话与设备**：同浏览器多账号、登出与会话撤销、已识别设备、登录事件。
- **管理员功能**：账号管理（搜索、排序、计数、备注、全部状态动作）、审批通知邮件，以及**面向账号的群发与定时邮件**。
- **设置与偏好**：账号相关的全部运行时设置、可插拔邮件与短信供应商、账号界面语言。

**用户确认留在上游模型服务的**：推理 API key 与套餐额度。Agent 的推理走用户自己的模型连接，计费在供应商侧。Agent 不建本地计费或推理 key 体系，逐项边界见 §五。

**不变的边界**：
- 不共享 Gateway 数据库、token、密钥或供应商凭据，不 import `serving.*`，不引入共享包。
- 所有新路由只在 `AGENT_AUTH_MODE=local` 下开放，Gateway 模式行为不变。由 `test_gateway_mode_serves_none_of_the_local_account_routes` 覆盖。
- 沙箱网络与凭据边界不变。

## 二、关键适配决定

1. **身份与准入分离。**
   - 上游的 `pending_approval/rejected` 是账号状态；Agent 用独立的准入（waiting list）状态表达。
   - 上游的"自动批准"，在 Agent 中是身份验证完成时由策略执行 `approve`，审计 actor 记为 `signup-policy`。
   - 待审账号可以登录、查看申请状态，这是有意偏离。
2. **身份表加访问视图，fail-closed。**
   - `0108_local_account_parity` 把旧表拆成 `agent_local_login_identities`（email、login_name、phone 各一行）和可选的 `agent_local_password_credentials`，数据全部保留。
   - 视图 `agent_local_account_access` 用 `LEFT JOIN` 加 `coalesce` 构造，缺任何一行即无权。所有 SQL 闸门都读它。
   - 每个身份记录 `verified_by`，取值为 `link`、`code`、`operator` 或 `policy`。只有 `link` 和 `operator` 算作**已证明**邮箱。管理员邮箱名单提权和注册域名自动批准都要求已证明（N1）。
3. **注册与账号配置是管理员设置。** 设置位于 `Admin → Settings → Accounts`，保存在数据库，首次从环境变量导入；密钥只写。共 55 项，见 `accounts/settings_registry.py`，并有测试保证每项都在设置页的分组里。
4. **删号是软删加终止栅栏。**
   - 新增 `deleted` 状态。删除时在同一事务内撤销会话、移出所有浏览器、作废令牌和手机验证码、撤销准入、写审计和终止记录。
   - 彻底擦除还需满足三个前置：工作已停、主机已清理、共享团队已移交。擦除后保留墓碑。
5. **多账号就是浏览器会话**，每个浏览器最多 5 个账号。
   - 每次请求解析到浏览器的当前账号。前端每个请求都带 `X-Agent-Account`，不符即返回 409 `account_switched`。
   - 已打开的流每 30 秒复查一次。
6. **已识别设备**沿用上游的 HMAC cookie。密钥由 `AGENT_LOCAL_SESSION_SECRET` 经 HKDF 派生，cookie 为 `__Host-` 前缀、`Path=/`。
7. **手机号**按上游实际能力实现：注册、登录、绑定（仅限尚未绑定时）、无密码账号的本人确认。短信供应商为接口加腾讯云实现，默认关闭；测试只用假供应商。
8. **邮件供应商**是 `smtp`、`tencent_ses` 或注册进来的传输方式，每封邮件发送时重新读取设置。SES 无法发送的邮件在入队前就被拒绝（N8）。
9. **cookie 与会话参数**：
   - 上游 `samesite`、`secure`、`domain` 可配置；Agent 固定使用 `__Host-` 名，这要求 Secure、`Path=/`、不得设置 Domain，是更严格的安全选择。
   - 会话有效期是设置项 `accounts.session_ttl_days`。
   - 上游的 JWT access-token 有效期在 Agent 中没有对应物（见 G14）。

## 三、功能对齐表

**状态**：✅ 已实现并有测试；➖ 不适用，同时写明原因。
**上游路径**相对于 `apps/backend/serving/`。

**测试缩写**：

| 缩写 | 文件 |
|---|---|
| U | `tests/unit/test_local_account_parity.py` |
| S | `tests/integration/storage/test_local_account_parity_store.py` |
| F | `tests/integration/servers/test_local_account_parity_flow.py` |
| RV | `tests/integration/storage/test_local_account_review_store.py` |
| MD | `tests/unit/test_local_account_mail_delivery.py` |
| RL | `tests/servers/test_login_rate_limits.py` |
| LF | `tests/integration/servers/test_local_accounts_flow.py` |
| LS | `tests/integration/storage/test_local_account_store.py` |
| web:AP | `AccountParity.test.tsx` |
| web:AC | `AccountCaptcha.test.tsx` |
| web:AA | `AccountAdministration.test.tsx` |
| web:PE | `AccountsPageErase.test.tsx` |
| web:LS | `AccountLocaleSync.test.tsx` |

### 3.1 注册

| # | 上游能力 | 上游来源 | Agent 实现 | 前端入口 | 测试 | 状态 |
|---|---|---|---|---|---|---|
| R1 | 邮箱加密码注册，密码强度规则 | `auth_routes.py` `signup`；`utils/password.py` | `POST /v1/auth/signup`；`application/accounts.py` | `/signup` | LF；web:AP | ✅ |
| R2 | 昵称 `user_name`、用途 `use_case` | `SignupRequest` | `use_case` 随注册提交准入申请 | `/signup` 资料步骤 | web:AP "asks for the Terms first…" | ✅ |
| R3 | 必须同意 ToS，前端两步确认，提供 `/terms` 页 | `auth_routes.py:231`；`SignupConsentStep.tsx` | 记录 `tos_accepted_at`；`terms_url` 或 `terms_text` 设置；两项都为空时仍要求勾选（G12） | `/signup` 同意步骤；`/terms` | F `test_sign_up_requires_the_terms_a_captcha_and_an_allowed_domain` | ✅ |
| R4 | Turnstile，用于注册和每一次发验证码 | `utils/turnstile.py`；`TurnstileChallenge.tsx` | `accounts/captcha.py`，未配置密钥时不校验，校验失败即拒绝 | 注册页，以及每个手机验证码表单；每个 token 只用一次 | U captcha 3 项；F 同上；web:AC 5 项 | ✅ |
| R5 | 拒绝一次性或保留域名 | `utils/email_blocklist.py` | 注册和改绑邮箱时检查 | — | U `test_disposable_and_reserved_domains_are_refused` | ✅ |
| R6 | 公开注册开关 | `signup_policy.py` | 设置 `signup_enabled`，同时作用于邮箱和手机注册 | 注册关闭页 | web:AP "says sign-up is closed…" | ✅ |
| R7 | 邮箱验证要求开关 | `runtime_settings.py` | `require_email_verification`；关闭时邮箱被接受但未证明（`policy`） | — | F `test_without_smtp_sign_up_works_once_verification_is_off`；RV N1 两项 | ✅ |
| R8 | 域名允许名单（精确或 `*.` 通配）与自动批准 | `signup_policy.py`；`admin/signup_domains.py` | `agent_local_signup_domains`；`/v1/admin/accounts/signup-domains`；只认已证明的邮箱 | `/admin/accounts/policy` | U domain 4 项；S wildcard；F domain policy；RV | ✅ |
| R9 | 注册 IP 限流（每小时 5 次、每天 10 次）和未解析来源分支（G11） | `signup_rate_limit.py` | `signup_rules`：共享额度，或返回 503 | — | RL 2 项（G11） | ✅ |
| R10 | 新注册通知管理员（开关、收件人），每个申请只通知一次 | `send_new_registration_admin_email` | `signup_admin_notify`、`signup_notify_emails`；在身份验证时通知 | 设置页 | RV N4 | ✅ |
| R11 | 用已注册邮箱再注册不泄露信息 | `auth_routes.py:257` | 统一返回 `{ok:true}`，并向该地址发送重置链接（比上游更严格） | — | LF | ✅ |
| R12 | 注册审计 | `auth_routes.py:330` | `agent.local_account.signup` | `/admin/audit` | S | ✅ |
| R13 | 发行版硬性禁止注册（G13） | `distribution.features.public_signup` | Agent 没有发行版概念 | — | — | ➖ |

### 3.2 邮箱验证

| # | 上游能力 | 上游来源 | Agent 实现 | 前端入口 | 测试 | 状态 |
|---|---|---|---|---|---|---|
| V1 | 验证链接（24 小时、单次） | `verify_email` | `POST /v1/auth/verify-email`；令牌放在 URL fragment；已用链接直接拒绝（偏离） | `/verify-email` | LS | ✅ |
| V2 | 重发验证邮件，不泄露 | `resend_verification` | `POST /v1/auth/resend-verification` | `/resend-verification` | LS | ✅ |
| V3 | 验证后按策略自动批准 | — | `after_verified_identity` | — | F；RV | ✅ |

### 3.3 登录

| # | 上游能力 | 上游来源 | Agent 实现 | 前端入口 | 测试 | 状态 |
|---|---|---|---|---|---|---|
| L1 | 邮箱加密码登录，反枚举 | `login` | `POST /v1/auth/login` | `/login` | LF | ✅ |
| L2 | 登录名加密码登录 | `auth_routes.py:439` | `identifier` 不含 `@` 时按登录名处理 | `/login` | F `test_a_login_name_account_signs_in_without_any_email` | ✅ |
| L3 | 手机号加验证码登录 | `phone_login` | `POST /v1/auth/phone/login` | `/login` 的手机标签页 | F phone；web:AP；web:AC | ✅ |
| L4 | 停用账号登录时显示停用说明 | `AccountSuspendedError` | 403 `account_suspended`，消息为管理员填写的说明 | `/login` | F administrators… | ✅ |
| L5 | 登录限流三个桶，先通过地址桶才计入账号桶 | `login_rate_limit.py` | `enforce_login_limits`，三项都可设置 | — | RL | ✅ |
| L6 | 已识别设备的独立限流桶 | `device_cookie.py` | `accounts/device_cookie.py` | — | U device 3 项；F `test_a_recognized_browser_signs_in…` | ✅ |
| L7 | 登录事件 | `record_login_event` | `agent_local_login_events` | 审核面板中的登录记录 | S `test_sign_in_events_are_recorded_and_purged…` | ✅ |
| L8 | 清理登录事件（按天数、按账号），需要确认 | `admin/login_events.py` | `DELETE /v1/admin/accounts/login-events` | 策略页；审核面板（F6 增加确认） | S；web:AA | ✅ |
| L9 | rehash 并发时 CAS 失败方不误报 401 | — | `_current_hash` 重新读取 | — | U rehash 2 项 | ✅ |
| L10 | 请求取消时不提前释放 hash 槽位 | — | `accounts/passwords.py` | — | U slot 2 项 | ✅ |

### 3.4 会话与多账号

| # | 上游能力 | 上游来源 | Agent 实现 | 前端入口 | 测试 | 状态 |
|---|---|---|---|---|---|---|
| S1 | 登出当前账号，其他账号保留 | `logout` | `POST /v1/session/logout` | 账号菜单 | F browser | ✅ |
| S2 | 登出本浏览器的全部账号 | `logout_all` | `POST /v1/auth/logout-all`，先锁浏览器（N6） | `/accounts` | S；RV N6 两项 | ✅ |
| S3 | 浏览器多账号（最多 5 个）：列表、切换、移除，显示准入状态 | `browser_sessions.py`；`AccountChooser.tsx` | `/v1/auth/accounts`、`/switch` | `/accounts`；登录页的"使用已登录账号"链接 | S；F；web:AP（含跨标签页刷新，F10） | ✅ |
| S4 | 其他标签页换号后的保护 | `accountSync.ts` | `X-Agent-Account` 加 409 `account_switched`，流每 30 秒复查 | `client.ts` `pinAccount` | F `test_two_accounts_share_a_browser…`；`accountPin.test.ts` | ✅ |
| S5 | 会话过期，以及改密、重置、停用、删号时撤销会话 | `session_live` | 服务器端会话行 | — | LS；S | ✅ |
| S6 | access token 加 refresh 轮换架构（G14） | `client.ts:140-190` | Agent 用服务器端会话 cookie，没有 access token；S4 的 pin 加 409 覆盖了上游"刷新时发现换号就停止"的保护 | — | — | ➖（架构差异） |

### 3.5 密码

| # | 上游能力 | 上游来源 | Agent 实现 | 前端入口 | 测试 | 状态 |
|---|---|---|---|---|---|---|
| P1 | 忘记密码与重置 | `auth_routes.py` | 已有；只在重置首次让身份变为已验证时通知管理员（N4）；重置证明了 `policy` 邮箱时即为收回账号（R3），重置页面和邮件 `account_reclaimed` 会告知清除了什么、需重新申请（T2） | `/forgot-password`、`/reset-password` | `test_local_session_reset_race.py`；RV | ✅ |
| P2 | 已登录时修改密码 | `change_password` | `POST /v1/auth/account/password` | 设置 → 账号 | F `test_changing_the_password…`；web:AP | ✅ |
| P3 | 无密码账号凭验证码设置首个密码 | `reauth.py` | 同上，用途为 `set_password` | 设置 → 账号 | S；web:AC | ✅ |
| P4 | 密码已更改通知 | `send_password_changed_email` | 邮件类型 `password_changed` | — | U render | ✅ |

### 3.6 邮箱与手机号绑定

| # | 上游能力 | 上游来源 | Agent 实现 | 前端入口 | 测试 | 状态 |
|---|---|---|---|---|---|---|
| B1 | 绑定或更换邮箱（G16）：先确认本人再查重，点击链接后才生效，通知旧地址，作废旧的重置链接 | `bind_email` | `POST /v1/auth/account/email`；令牌带 `pending_email`；无 SMTP 时返回 503 | 设置 → 账号 | F `test_a_new_email_takes_effect_only_from_its_link…`；S binding 2 项 | ✅ |
| B2 | 邮箱已更改通知 | `send_email_changed_email` | `email_changed` | — | F | ✅ |
| B3 | 绑定手机号（仅限尚未绑定时） | `bind_phone` | `POST /v1/auth/account/phone` | 设置 → 账号（captcha 见 F1） | F phone；S race；web:AC | ✅ |
| B4 | 解绑或更换手机号 | 上游没有 | — | — | — | ➖（上游无此能力） |

### 3.7 手机认证基础

| # | 上游能力 | 上游来源 | Agent 实现 | 前端入口 | 测试 | 状态 |
|---|---|---|---|---|---|---|
| H1 | 按用途发送验证码、冷却与配额、不泄露号码是否注册 | `send_phone_code` | `POST /v1/auth/phone/code` | `PhoneCodeFields` | U 6 项；S codes 4 项 | ✅ |
| H2 | 手机号注册 | `phone_signup` | `POST /v1/auth/phone/signup` | `/signup` 的手机标签页 | F；web:AP；web:AC | ✅ |
| H3 | 号码规范化、默认国家码、允许的前缀 | `normalize_phone` | `accounts/sms.py` | — | U | ✅ |
| H4 | 验证码只存 keyed hash，有尝试上限，先预留后发送 | `phone_auth.py` | `agent_local_phone_codes` | — | U；S | ✅ |
| H5 | 短信供应商接口和腾讯云实现 | `utils/sms.py`；`tencent_cloud/sms.py` | `accounts/sms.py`、`sms_tencent.py`、`tencent_cloud.py` | 设置页"Phone sign-in" | U tencent 3 项（mock 传输） | ✅ |
| H6 | 前端登录、注册方式选择 | `AuthMethodSelector.tsx` | 标签页切换 | `/login`、`/signup` | web:AP | ✅ |

### 3.8 删号与擦除

| # | 上游能力 | 上游来源 | Agent 实现 | 前端入口 | 测试 | 状态 |
|---|---|---|---|---|---|---|
| D1 | 自助删号：输入标识并证明本人；管理员不能删除自己；软删；发通知 | `delete_own_account` | `POST /v1/auth/account/delete` | 设置 → 账号的"删除账号"区块 | F deleting…；U；web:AP | ✅ |
| D2 | 管理员软删 | `delete_user` | 管理动作 `delete` | `/admin/accounts` | S；F | ✅ |
| D3 | 管理员恢复 | `resume_user` | `resume`，等清理确认后才能执行；已擦除的账号不能恢复 | 同上 | U；S | ✅ |
| D4 | 管理员彻底删除，含清除清单和写入栅栏（G1） | `hard_delete_user`、`erasure_fence` | `POST /{id}/erase`：按外键遍历删除全部 owner 数据，不触碰其他 owner 的行，墓碑同时充当栅栏；要求主机已清理（N2），共享团队已移交（N3） | 擦除区块，结果在页面上保留（F3） | S erasing 5 项；RV N2、N3；F；web:PE | ✅ |
| D5 | 账号已删除通知 | `send_account_deleted_email` | `account_deleted` | — | U render | ✅ |
| D6 | 删号时清理手机验证码（G17） | `_forget_phone_challenges` | 删除时删除该账号的验证码；擦除时再清理一次 | — | RV `test_deleting_an_account_forgets_its_phone_codes` | ✅ |

### 3.9 管理员

| # | 上游能力 | 上游来源 | Agent 实现 | 前端入口 | 测试 | 状态 |
|---|---|---|---|---|---|---|
| A1 | 列表过滤、分页、按创建或最近登录排序（G10） | `list_users` | `GET /v1/admin/accounts` 的 `sort` 参数 | `/admin/accounts` | S sort；`page.test.tsx` | ✅ |
| A2 | 搜索 | `search` | `?q=` | 搜索框，300 毫秒防抖 | S search | ✅ |
| A3 | 批准、拒绝、停用、恢复、授予或撤销管理员 | `approve_user` 等 | 9 个动作，带版本号；管理员名单会重新授予的撤销操作被拒绝（N10、R2） | 审核面板，有中文界面（F11） | `test_local_access_termination.py`；F；RV R2；web:AA | ✅ |
| A4 | 批准和拒绝结果邮件 | `send_approval_email` | `approved`、`rejected` | — | F | ✅ |
| A5 | 管理员备注、停用说明 | `update_user` | `PATCH /{id}/notes` | 审核面板 | F；web:AA | ✅ |
| A6 | 详情字段（G10） | `get_user_detail` | 标识、最近登录、审核人、用途、备注 | 列表行 | F | ✅ |
| A7 | 审计（G9） | `list_audit_log` | 14 个上游动作各有对应，另加 reclaim、transfer_team、broadcast | `/admin/audit` | S；RV | ✅ |
| A8 | 状态计数 | `StatusCounts` | 响应中的 `counts` | 计数卡 | F | ✅ |
| A9 | `ADMIN_EMAILS` 登录自动提权（G3） | `auth_routes.py:527` | 设置项 `admin_emails`，只认已证明的邮箱（N1），名单优先（N10） | 设置页说明，动作说明 | F 3 项；RV 4 项 | ✅ |
| A10 | 账号群发与定时邮件 | `admin/broadcast.py`、`email_scheduler` | `/v1/admin/account-mail`：预览、测试、创建或定时、取消、收件人明细；收件人在创建时固定；认领带令牌（R1）；每个收件人逐条记录（N5）；SES 发不出的邮件预先拒绝（N8）；不能以已删除账号为受众（N7） | `/admin/accounts/mail`：预览与内容绑定、发送前确认（F2）、分页（F8） | S broadcast 3 项；RV 4 项；F 2 项；MD；web:AA | ✅ |
| A11 | 移交已删除 owner 的团队 | — | `GET /{id}/teams`、`POST /{id}/teams/{team}/transfer` | 擦除区块中的"它拥有的团队" | RV；F；web:AA | ✅（Agent 特有） |

### 3.10 首次部署与运维建号

| # | 上游能力 | 上游来源 | Agent 实现 | 前端入口 | 测试 | 状态 |
|---|---|---|---|---|---|---|
| F1 | 一次性 setup code 创建登录名管理员 | `routers/setup.py` | `POST /v1/auth/setup/admin`，启动日志写明 `/setup/administrator` | `/setup/administrator`；登录页链接 | F setup 2 项；S setup 2 项；web:AP | ✅ |
| F2 | 运维 CLI 建号或提权，无需 SMTP（G15） | `ops/admin/create_admin.py` | `bootstrap_admin --create-email/--create-login-name/--member/--approve`，记为 `operator` | — | S `test_the_operator_creates…`；RV | ✅ |
| F3 | 公开配置端点（G4） | `/site-config` | `GET /v1/auth/options`；`/config` 保持不变 | 登录页和注册页 | F；web:AP | ✅ |

### 3.11 个人资料与偏好

| # | 上游能力 | 上游来源 | Agent 实现 | 前端入口 | 测试 | 状态 |
|---|---|---|---|---|---|---|
| U1 | 账号信息 | `get_current_user_info` | `/v1/session/me` 的 `identity` 字段；`GET /v1/auth/account` | 设置 → 账号 | F | ✅ |
| U2 | 昵称、头像 | `update_profile` | Agent 已有 Profile 功能；注册时的 `user_name` 存在账号上 | — | 既有测试 | ✅（形状不同） |
| U3 | 界面语言随账号保存（G7），以最新选择为准，未登录时的选择带上 `chosen_at` | `PUT /user/preferences/locale`；`LocaleChoice {owner, stored}` | `PUT /v1/auth/account/locale`，返回 `locale_chosen_at`；浏览器记录选择的归属与是否已存储，只把未登录或同一账号的选择带入账号 | `AccountLocaleSync` | F language；web:LS 4 项；`AccountLocaleSharedBrowser.test.tsx` 4 项（同一浏览器多账号互不覆盖，L1） | ✅ |

### 3.12 设置与供应商（G5、G6）

| # | 上游能力 | Agent 实现 | 测试 | 状态 |
|---|---|---|---|---|
| C1 | 登录限流 3 项、注册限流 2 项、未解析来源 3 项、短信 13 项、Turnstile、SMTP 6 项加 `email_provider`、通知收件人 | 全部放进 Accounts 设置（55 项），只写密钥加密保存 | `test_account_settings_groups.py`；U | ✅ |
| C2 | cookie 的 `samesite`、`secure`、`domain` | 固定使用 `__Host-`（见 §二第 9 条） | U device | ➖（有意固定） |
| C3 | 可插拔邮件供应商，包括腾讯云 SES 模板发送 | `accounts/delivery.py`、`ses_tencent.py`、`register_email_provider` | MD（mock 传输） | ✅ |

## 四、已知原 copy 问题（已修复）

| 问题 | 处理 | 测试 |
|---|---|---|
| 取消 await 时过早释放 hash 槽位 | 槽位在 executor 任务结束时才释放 | U |
| `needs_rehash` 并发时 CAS 失败方返回 401 | CAS 返回成败；失败方重新读取并重新校验 | U |
| 登录限流先过 per-IP 才计入账号桶 | 已核对，并增加设备桶 | RL |
| 邮件限流键与登录规范化不一致 | 统一使用规范化标识 | U |

## 五、上游计费与推理面边界（G2，用户确认留在上游）

| 上游能力 | 用途 | Agent 对应边界 |
|---|---|---|
| 角色 `free/pro/internal/admin` | 模型可见性、额度、并发 | Agent 只有 `agent.use` 和 `agent.admin` 两级准入，"授予/撤销管理员"已实现；没有付费分级 → ➖。管理员自我保护规则保留：不能自降、自停、自删。 |
| 每用户日额度、月额度，按角色的默认额度 | 计费限额 | 推理走用户自己的模型连接，计费在供应商侧 → ➖ |
| 并发 `max_concurrent_requests` | 推理并发闸 | 由 Agent 调度器配置 → ➖ |
| `disabled_models` | 模型访问控制 | 模型来自用户连接 → ➖ |
| API key 生成、列表、删除、重生成 | 推理 API 认证 | Agent 没有公共推理 API → ➖。账号的凭据类对象（模型连接密钥）在停用和删号时随终止栅栏失效，擦除时被删除（S `test_erasing_removes_the_provider_key…`）。 |
| 用量、成本、最近请求、自动化评分 | 计费观测 | 本地模式没有 → ➖ |
| API-key 认证失败封禁 | 只保护推理 key 入口 | → ➖ |
| `llm-prober-layout` 偏好 | 推理调试页布局 | 不属于账号功能 → ➖ |

## 六、有意偏离

| 偏离 | 理由 |
|---|---|
| 待审账号可以登录，查看申请状态 | Agent 的准入独立于身份 |
| 已注册地址再注册时，不改账号状态，只发重置链接 | 不泄露，也不改动他人账号 |
| 已用过的验证链接直接拒绝，不报告其状态 | 不向持有旧链接的人透露账号信息 |
| 限流计数器持久化在 Postgres，令牌放在 URL fragment | 多副本共享计数；令牌不进入访问日志 |
| 首次设置等到管理员出现为止，不关闭注册 | 由已存在的管理员关闭首次设置 |
| `require_email_verification=false` 时，旧的未验证账号仍需验证 | fail-closed |
| 关闭验证期间接受的邮箱不被管理员名单和域名名单信任，直到证明为止；重置密码即收回账号 | 上游在此存在提权漏洞（N1），不照搬。收回时无法区分抢注者和真正自己注册的人，因此清除注册资料、撤回待审申请，并在重置页和邮件中明确告知；已作出的审批决定和账号中已有的 Agent 数据保留（T2） |
| 管理员名单优先，拒绝它会撤回的撤销操作 | 不报告实际不会持久的撤销（N10） |
| 擦除保留墓碑；共享团队先移交；保留的 id 列见开发文档 | 不误删其他成员的数据（N3） |
| 群发只发给已验证地址；Markdown 不渲染原始 HTML；SES 发不出的邮件预先拒绝 | 安全，且失败不再静默（N8） |
| 沙箱删除归档留在存储桶中，按保留策略处理 | 控制平面无权删除这些对象 |
| 不能解绑或更换手机号 | 与上游一致 |
| 管理页的中文只覆盖新增的账号管理页面 | 控制台其余部分仍为英文（F11） |

## 七、迁移说明

`0108_local_account_parity` 在开发期间曾原地扩展，加入了 locale、群发、`verified_by`、`claim_token`。早期版本只应用于任务隔离的测试库，从未发布或部署（N9）。基于旧快照建立的任务库需要重建；`make test-db-local` 每次运行都会新建数据库。0107→0108 的数据保留迁移测试仍然保留，见 S `test_the_migration_carries_accounts_over_and_refuses_a_lossy_downgrade`。

## 八、真实环境验收缺口

以下没有获得授权，也没有执行，只在隔离环境中用假供应商、mock 传输或合成数据验证：

- 真实 SMTP 投递。
- 腾讯云 SES 的实际模板审核与发送。
- 腾讯云短信的实际发送。
- Cloudflare Turnstile 的真实挑战，前端测试用的是替身。
- 生产数据库的 0107→0108 升级。
- 生产部署与生产账号的修改。
- 真实浏览器对真实 API 的 smoke 只覆盖合成账号和本机隔离环境，使用本地邮件 sink 和假短信，见交付文档。
