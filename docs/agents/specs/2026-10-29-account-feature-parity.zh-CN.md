# 本地账号与 HybridInference 账号功能对齐（feature parity）

日期：2026-10-29
状态：**草稿**——功能清单与适配决定已定，实现逐条推进；「状态」一列随提交更新。

比较源：HybridInference `origin/dev` `8b49872d1dcbed5c4bc8132b026b6740c3437aa7`（只读）。
原始 copy 来源 `321e9a6c`。Cloud Agent 起点：PR #1030 head `506f33fe` 合入 dev `4b7b826d`。

本文取代 [本地登录身份设计](2026-10-29-local-login-identities-design.zh-CN.md) 与
[身份范围评审](../reviews/2026-10-29-identity-scope-review.zh-CN.md) 里的**分期与排除决定**：
手机号、改绑邮箱、删号、多账号、已识别设备、ToS、captcha、域名策略全部在本次范围内。
那两份文件里经核实成立的证据与设计（身份表 + `LEFT JOIN` 访问视图、限流分桶）继续沿用。

## 一、范围

**在范围内**：HybridInference 账号系统的全部能力——注册、验证、登录（邮箱、登录名、手机号）、
找回与修改密码、邮箱与手机号绑定、ToS、captcha、注册策略（开关、邮箱验证要求、域名
允许名单与自动批准、一次性邮箱拒绝）、首次部署建管理员、删号（自助与管理员，软删、恢复、
彻底删除）、同浏览器多账号、登出与会话撤销、已识别设备限流、登录事件、管理员账号管理与
审批通知邮件、账号相关配置。

**不在范围内**（不是账号系统）：推理调度、计费与额度（API key、角色额度 `free/pro/internal`、
`max_concurrent_requests`、`disabled_models`、用量统计、成本历史、自动化评分）、
API key 认证失败封禁（`auth_failure_blocklist`、`/admin/auth-blocks`——只保护推理 API key 入口，
Agent 没有 API key）、营销群发邮件（`/admin/broadcast`、`email_scheduler`）、
Gateway↔Agent 的 OAuth 同意页（`/authorize`，Agent 的 Gateway 模式已经在用，本地模式没有对应物）、
推理会话标识（`session_identity`）。

**不变的边界**：不共享 Gateway 数据库、token、密钥或供应商凭据，不 import `serving.*`，不引入共享包。
所有新路由只在 `AGENT_AUTH_MODE=local` 下开放，Gateway 模式行为不变；沙箱网络与凭据边界不变。

## 二、关键适配决定

1. **身份与准入分离（保留）。** 上游的 `pending_approval/rejected` 是账号状态，Agent 是独立的
   准入（waiting list）状态。上游「自动批准」= Agent 在身份验证完成时由策略自动 `approve`
   （审计 actor `signup-policy`）。上游 pending 账号不能登录；Agent 保持「能登录、看申请状态」
   的有意偏离。
2. **身份表 + 访问视图，fail-closed。** 新迁移 `0108_local_account_parity` 把
   `agent_local_password_logins` 拆成 `agent_local_login_identities`（email / login_name / phone，
   一类一行）和 `agent_local_password_credentials`（可选），数据保留。准入判断收敛为视图
   `agent_local_account_access`（`LEFT JOIN` + `coalesce`）：缺任何一行即无权。
   「身份已验证」= 没有未验证的邮箱身份，且至少有一个已验证身份（login_name 由运维创建即已验证，
   手机号只在验证码通过后写入）。原来硬编码 `email_verified` 的 7 处（Python 3 处、SQL 4 处）全部改读它。
3. **注册开关按上游语义成为管理员设置**（`Admin → Settings → Accounts`，数据库保存，首次从环境变量导入）：
   `signup_enabled`、`require_email_verification`（关闭时新注册邮箱视为已验证，因而无 SMTP 也能注册；
   已有未验证账号仍需验证——偏离上游「登录时放行」，fail-closed）、`signup_admin_notify`、
   通知收件人、`approval_policy`（`manual` 为 Agent 现状默认；`domains` 完全按上游：
   允许名单为空 = 全部自动批准，否则名单内域名自动批准）。
4. **删号是软删 + 终止栅栏。** 状态新增 `deleted`：同一事务撤销会话、移出所有浏览器、作废令牌、
   撤销准入、写审计和 `agent_local_access_terminations` 记录（action `delete`），之后走既有
   `terminate_owner_access` 停止排队/运行/驻留工作与终端；记录未闭合前不能恢复。
   管理员恢复（`resume`）只恢复登录，准入保持 revoked。彻底删除（hard delete）要求已软删且终止已完成。
5. **多账号 = 浏览器会话**（上游 `browser_sessions`，最多 5 个）。cookie 仍一个，值改为浏览器令牌。
   每次请求解析到浏览器的**当前账号**；前端每个请求带 `X-Agent-Account`（流式连接用查询参数），
   与当前账号不符即 409 `account_switched`，避免标签页以错 owner 写入。已开的 SSE/WebSocket
   在 30 s 复查时发现 cookie 已指向另一账号即结束，不迁移。
6. **已识别设备**：上游无状态 HMAC cookie（`device_cookie.py`），密钥改为 `AGENT_LOCAL_SESSION_SECRET`
   的 HKDF 派生；限流仍在 Postgres。
7. **手机号**：按上游实际能力——验证码注册、登录、绑定（仅未绑定时；上游没有解绑和更换），
   以及无密码账号用手机验证码确认敏感操作。OTP 一次性、每码尝试上限、发送冷却与配额按上游参数；
   计数复用 Postgres 限流表（键为 keyed hash），验证码只存 keyed hash。
   短信供应商：适配器接口 + 腾讯云实现（照上游 TC3 签名），默认关闭；测试只用假供应商与 mock 传输。
8. **SMTP 与 captcha 配置**也进 Accounts 设置（密钥加密、只写）。Turnstile 未配置密钥即不校验（上游语义）。
9. **一次性/保留域名拒绝**：上游硬编码列表照搬，适用于注册与改绑邮箱。

## 三、功能对齐表

状态：✅ 已实现并有测试 ｜ 🔧 本分支实现中 ｜ ⬜ 待实现 ｜ ➖ 不适用（说明原因）。
上游路径相对 `apps/backend/serving/`（后端）或 `apps/frontend/src/`（前端）。

### 3.1 注册

| # | 上游能力 | 上游来源 | Agent 入口 / 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| R1 | 邮箱+密码注册，密码强度规则 | `servers/routers/auth_routes.py` `signup`；`utils/password.py` | `POST /v1/auth/signup`；`application/accounts.py` | `test_local_accounts_flow.py` | ✅（PR #1030） |
| R2 | 注册填写昵称 `user_name`、用途 `use_case` | `schemas_auth.py` `SignupRequest` | signup 增字段；`use_case` 写入准入申请（需审批时直接 pending） | 待补 | ⬜ |
| R3 | 必须同意 ToS（`accepted_tos`），前端条款确认步骤 | `auth_routes.py:231`；`app/signup/SignupConsentStep.tsx`、`app/terms` | signup/phone signup 校验并记录 `tos_accepted_at`；前端勾选 + 条款链接（`terms_url` 设置） | 待补 | ⬜ |
| R4 | Turnstile captcha（注册、发验证码） | `utils/turnstile.py`；`components/auth/TurnstileChallenge.tsx` | `accounts/captcha.py`；设置 `turnstile_site_key/secret_key`；前端组件 | 待补 | ⬜ |
| R5 | 一次性/保留邮箱域名拒绝 | `utils/email_blocklist.py` | 注册与改绑邮箱 | 待补 | ⬜ |
| R6 | 公开注册开关（运行时） | `auth/signup_policy.py` `is_public_signup_enabled` | 设置 `signup_enabled`（原 `AGENT_LOCAL_SIGNUP_ENABLED` 导入） | 待补 | ⬜ |
| R7 | 邮箱验证要求开关（运行时） | `config/runtime_settings.py` `signup_require_email_verification` | 设置 `require_email_verification` | 待补 | ⬜ |
| R8 | 域名允许名单（精确 + `*.` 通配）与自动批准，管理员增删 | `auth/signup_policy.py`；`routers/admin/signup_domains.py`；`dashboard/admin/SignupPolicySection.tsx` | 表 `agent_local_signup_domains`；`/v1/admin/accounts/signup-domains`；设置 `approval_policy`；管理页 | 待补 | ⬜ |
| R9 | 注册 IP 限流（5/h、10/d） | `utils/signup_rate_limit.py` | Postgres 计数 | `test_login_rate_limits.py` | ✅ |
| R10 | 新注册通知管理员邮件（开关、收件人） | `utils/email.py` `send_new_registration_admin_email`；`get_signup_notify_emails` | 设置 `signup_admin_notify`、`signup_notify_emails`（空则发给当前本地管理员） | 待补 | ⬜ |
| R11 | 已注册邮箱再注册不泄露（上游 409；Agent 更严：发重置链接） | `auth_routes.py:257` | 保持 Agent 行为；已删除账号不发信 | 待补 | 🔧 |
| R12 | 注册审计（anonymous actor） | `auth_routes.py:330` | `agent_admin_audit_log` `agent.local_account.signup` | 待补 | ⬜ |

### 3.2 邮箱验证

| # | 上游能力 | 上游来源 | Agent 入口 / 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| V1 | 验证链接（24h、单次） | `auth_routes.py` `verify_email` | `POST /v1/auth/verify-email`（fragment 令牌） | `test_local_account_store.py` | ✅ |
| V2 | 重发验证（不泄露） | `auth_routes.py` `resend_verification` | `POST /v1/auth/resend-verification` | ✅ | ✅ |
| V3 | 验证后按策略自动批准 | `signup` 初始状态 | 验证事务内按 `approval_policy` 自动 approve | 待补 | ⬜ |

### 3.3 登录

| # | 上游能力 | 上游来源 | Agent 入口 / 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| L1 | 邮箱+密码登录，反枚举 | `auth_routes.py` `login` | `POST /v1/auth/login` | `test_local_accounts_flow.py` | ✅ |
| L2 | 登录名（无邮箱）+密码登录 | `auth_routes.py:439` | login 的 `identifier` 不含 `@` 即按登录名 | 待补 | ⬜ |
| L3 | 手机号+验证码登录 | `phone_auth_routes.py` `phone_login` | `POST /v1/auth/phone/login` | 待补 | ⬜ |
| L4 | 停用账号登录时显示管理员填写的停用说明 | `exceptions.AccountSuspendedError`；`suspension_message` | 403 `account_suspended` 带 `message` | 待补 | ⬜ |
| L5 | 登录限流三桶 + 先过地址桶才计账号桶 | `utils/login_rate_limit.py` | `api/auth.py` `enforce_login_limits` | `test_login_rate_limits.py` | ✅ |
| L6 | 已识别设备（device cookie）独立限流桶 | `utils/device_cookie.py`；`login_rate_limit.py` device | `accounts/device_cookie.py`；login 与重新认证使用 | 待补 | ⬜ |
| L7 | 登录事件（成功/失败原因/IP/UA） | `storage/...record_login_event`；`phone_auth.record_phone_login_event` | 表 `agent_local_login_events` | 待补 | ⬜ |
| L8 | 登录事件清理（按天数 / 按账号） | `routers/admin/login_events.py` | `DELETE /v1/admin/accounts/login-events`；管理页按钮 | 待补 | ⬜ |
| L9 | 需要 rehash 时并发登录不误 401 | （上游无此竞态：passlib 同步） | 修复 CAS 失败方 | 待补 | ⬜ |
| L10 | 取消请求不提前释放 hash 槽位 | — | `accounts/passwords.py` | 待补 | ⬜ |

### 3.4 会话与多账号

| # | 上游能力 | 上游来源 | Agent 入口 / 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| S1 | 登出当前账号（其他账号保留） | `auth_routes.py` `logout` | `POST /v1/session/logout` | 待补 | 🔧 |
| S2 | 登出本浏览器全部账号 | `auth_routes.py` `logout_all` | `POST /v1/auth/logout-all` | 待补 | ⬜ |
| S3 | 浏览器多账号（最多 5）列表、切换、移除 | `auth/browser_sessions.py`；`/auth/accounts`、`/auth/switch`、`DELETE /auth/accounts/{id}`；`components/auth/AccountChooser.tsx` | 表 `agent_local_browser_sessions`、`agent_local_browser_accounts`；同名路由；账号菜单 | 待补 | ⬜ |
| S4 | 切换后其他标签页提示/重载 | `lib/auth/accountSync.ts`；`AccountChangedNotice.tsx` | `X-Agent-Account` + 409；`SessionSwitchWatcher` 扩展 | 待补 | ⬜ |
| S5 | 会话过期、密码重置/停用/删号撤销会话 | `browser_sessions.session_live` | 服务器端会话行 | `test_local_account_store.py` | ✅（删号待补） |

### 3.5 密码

| # | 上游能力 | 上游来源 | Agent 入口 / 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| P1 | 忘记密码 / 重置（1h，作废其他链接，撤销全部会话） | `auth_routes.py` | 已有 | `test_local_session_reset_race.py` | ✅ |
| P2 | 已登录改密码（验旧密码、计入登录限流、撤销其他会话、作废重置链接、通知邮箱） | `user_routes.py` `change_password` | `POST /v1/account/password` | 待补 | ⬜ |
| P3 | 无密码账号（手机号）用 `set_password` 验证码设首个密码 | 同上；`reauth.py` | 同上 | 待补 | ⬜ |
| P4 | 密码已更改通知邮件 | `utils/email.py` `send_password_changed_email` | mail kind `password_changed` | 待补 | ⬜ |

### 3.6 邮箱与手机号绑定

| # | 上游能力 | 上游来源 | Agent 入口 / 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| B1 | 绑定或更换邮箱：先确认本人（密码或手机验证码），新地址发验证链接，点击后才生效，通知旧地址，作废旧重置链接 | `auth_routes.py` `bind_email`、`verify_email` pending 分支；`components/auth/EmailBindForm.tsx` | `POST /v1/account/email`；令牌表增 `pending_email`；verify 分支 | 待补 | ⬜ |
| B2 | 邮箱已更改通知 | `send_email_changed_email` | mail kind `email_changed` | 待补 | ⬜ |
| B3 | 绑定手机号（验证码，未绑定时） | `phone_auth_routes.py` `bind_phone`；`storage/phone_auth.py` `bind_user_phone` | `POST /v1/account/phone` | 待补 | ⬜ |
| B4 | 解绑/更换手机号 | 上游**没有**（`bind_user_phone` 只允许 `phone IS NULL OR phone = $2`） | — | — | ➖ 上游无此能力 |

### 3.7 手机号认证基础

| # | 上游能力 | 上游来源 | Agent 入口 / 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| H1 | 发送用途绑定的验证码（signup/login/bind/delete_account/bind_email/set_password），captcha、冷却与每号/每 IP 配额，不泄露号码是否注册 | `phone_auth_routes.py` `send_phone_code`；`storage/phone_auth.py` | `POST /v1/auth/phone/code` | 待补 | ⬜ |
| H2 | 手机号注册（ToS、注册限流、策略、通知） | `phone_signup` | `POST /v1/auth/phone/signup` | 待补 | ⬜ |
| H3 | 号码规范化、默认国家码、允许前缀 | `utils/phone_auth.py` `normalize_phone` | `accounts/phone.py` | 待补 | ⬜ |
| H4 | 验证码 keyed hash、每码尝试上限、先预留后发送、供应商确认才激活 | `storage/phone_auth.py` | 表 `agent_local_phone_codes` | 待补 | ⬜ |
| H5 | 短信供应商接口与配置（默认关闭）+ 腾讯云实现 | `utils/sms.py`、`utils/delivery.py`、`integrations/tencent_cloud/sms.py`、`api.py` | `accounts/sms.py`、`accounts/sms_tencent.py`；Accounts 设置 | 待补 | ⬜ |
| H6 | 前端：登录/注册方式选择、手机表单、国家码 | `components/auth/AuthMethodSelector.tsx`、`PhoneAuthForm.tsx`、`lib/phone-countries.ts` | 同名组件 | 待补 | ⬜ |

### 3.8 删号

| # | 上游能力 | 上游来源 | Agent 入口 / 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| D1 | 自助删号：输入账号标识确认 + 密码（或手机验证码）；管理员不可自删；软删；通知邮件 | `auth_routes.py` `delete_own_account`；`components/features/auth/DeleteAccount.tsx` | `POST /v1/account/delete`；终止栅栏 | 待补 | ⬜ |
| D2 | 管理员软删（active/suspended），不可删自己 | `routers/admin/users.py` `delete_user` | admin action `delete` | 待补 | ⬜ |
| D3 | 管理员恢复已删账号 | `resume_user` | admin action `resume` | 待补 | ⬜ |
| D4 | 管理员彻底删除（需先软删，确认） | `hard_delete_user` | `POST /v1/admin/accounts/{id}/erase` | 待补 | ⬜ |
| D5 | 账号已删除通知邮件 | `send_account_deleted_email` | mail kind `account_deleted` | 待补 | ⬜ |

### 3.9 管理员

| # | 上游能力 | 上游来源 | Agent 入口 / 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| A1 | 列表按状态过滤、分页 | `admin/users.py` `list_users` | `GET /v1/admin/accounts` | `admin/accounts/page.test.tsx` | ✅ |
| A2 | 搜索（邮箱、登录名、手机号、昵称、id 前缀） | 同上 `search` | `?q=` | 待补 | ⬜ |
| A3 | 批准 / 拒绝（含原因）/ 停用 / 恢复 / 授予撤销管理员 | `approve_user`、`reject_user`、`update_user` | 已有 7 动作 | `test_local_access_termination.py` | ✅ |
| A4 | 批准、拒绝结果邮件通知用户 | `send_approval_email`、`send_rejection_email` | mail kind `approved`、`rejected` | 待补 | ⬜ |
| A5 | 管理员备注 `admin_note`、停用说明 `suspension_message` | `update_user` | admin action `annotate` | 待补 | ⬜ |
| A6 | 用户详情（标识、状态、最近登录） | `get_user_detail`（账号部分） | 列表项扩展 `last_login_at`、标识 | 待补 | ⬜ |
| A7 | 审计日志 | `list_audit_log` | 已有 `/admin/audit` | — | ✅ |
| A8 | 状态计数 | `StatusCounts` | 列表响应 `counts` | 待补 | ⬜ |

### 3.10 首次部署与运维建号

| # | 上游能力 | 上游来源 | Agent 入口 / 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| F1 | 首次部署：启动日志打印一次性 setup code，`/setup` 用它创建登录名管理员并登录；未完成前注册 503 | `servers/routers/setup.py`、`setup_state.py`；`app/setup/CreateAdminStep.tsx` | `GET /v1/auth/setup`、`POST /v1/auth/setup/admin`；登录页内的建管理员表单 | 待补 | ⬜ |
| F2 | 运维 CLI 建号（无 SMTP） | （上游只有 F1）| `python -m cloud_agent.accounts.bootstrap_admin --create-login-name` | 待补 | ⬜ |

### 3.11 个人资料

| # | 上游能力 | 上游来源 | Agent 入口 / 实现 | 测试 | 状态 |
|---|---|---|---|---|---|
| U1 | `/user/me` 账号信息（标识、是否有密码、手机） | `user_routes.py` `get_current_user_info` | `GET /v1/session/me` 扩展 | 待补 | ⬜ |
| U2 | 昵称、头像 | `update_profile` | Agent 既有 Profile（名、姓、头像） | 既有 | ✅（形状不同） |
| U3 | 界面语言偏好 | `update_locale_preference` | Agent 前端只有英文 | — | ➖ Agent 无多语言界面 |

## 四、已知原 copy 问题（一并修复）

| 问题 | 位置 | 处理 |
|---|---|---|
| 取消 await 过早释放 hash 槽位 | `accounts/passwords.py` `_run` | 槽位在 executor 任务结束时释放 |
| `needs_rehash` 并发 CAS 失败方 401 | `application/accounts.py` `sign_in` | CAS 返回是否成功；失败则重读并重验 |
| 登录限流先 per-IP 通过才计 account-wide | `api/auth.py` | PR #1030 已修，补设备桶后复核 |
| 邮件限流键与登录规范化不一致 | `api/auth.py` `_email_key` | 统一用规范化标识 |

## 五、真实环境验收缺口（实现完成后填写）

未获授权发送真实短信/邮件、生产部署或修改生产账号；这些只在隔离环境用假供应商验证。
</content>
