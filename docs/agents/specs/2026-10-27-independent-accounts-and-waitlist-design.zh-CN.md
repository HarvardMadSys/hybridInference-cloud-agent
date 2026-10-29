# Cloud Agent 独立账号与 waiting list

日期：2026-10-27；更新：2026-10-28。状态：独立账号、waiting list 与个人模型连接已整合并完成代码验收；
未合并、部署或迁移生产数据。

[整合验收记录](../reviews/2026-10-28-local-accounts-model-connections-acceptance.md)记录当前代码版本、测试环境、结果及未覆盖范围。

代码核对基线：Cloud Agent `3f60b6afbb86c67422b3c237c3e9ab68ed2effac`；
HybridInference `321e9a6c457dcc80d35977aaac472d54041204a8`。
两条工作线已在 PR #1030 整合，个人模型连接基于 PR #1029。

## 1. 已确认的产品方向

- Cloud Agent 拥有独立账号、独立申请与审批，不要求用户先拥有获批的 HybridInference 账号。
- 复用 HybridInference 已有登录系统的代码和交互，适配到 Cloud Agent；不另起一套邮箱验证码登录方案。
- 个人模型连接由用户配置，Cloud Agent 不提供共享模型、平台 key 或额外推理额度。
  费用和限额仍归所填 key 的上游账号；若使用同一 FreeInference 账号的 key，仍可能共用其额度。
  Cloud Agent 账号独立不等于上游额度独立。
- 身份与 waiting list、个人模型连接两条线并行推进；先明确产品与接口，再分别实现和集成。
- 复用代码不意味着共享用户数据库、会话、签名密钥或运行时 Python 导入。

首版沿用现有邮箱/密码注册登录、邮箱验证、找回密码。手机号登录、多账号切换、
以及「无邮箱的 login_name 账号」原计划不在此版；该范围决定已由
[本地账号的登录身份](2026-10-29-local-login-identities-design.zh-CN.md)取代，
后者把这些能力分期，并说明为什么 login_name 账号与无 SMTP 引导排在最前。
迁移历史账号的凭据或数据不是本文的执行授权。

## 2. 用户与管理员流程

1. 用户在 Cloud Agent 注册并验证邮箱，建立本地身份。
2. 提交 Agent 使用申请，可填写用途；登录后看到申请状态。
3. 管理员在 Agent 后台查看、批准或拒绝申请。批准只授予 `agent.use`。
4. 获批用户填写自己的 FreeInference 或其他兼容服务的 Base URL、API key 与模型；
   验证成功才保存，然后选择该连接创建任务、续聊和 Fork。
5. 管理员可以撤销 Agent 准入；停用账号还会撤销该账号的登录会话。
6. 只有显式授予 `agent.admin` 的用户可管理平台；不能依据 Gateway role 或邮箱匹配隐式提权。

| 账号/准入状态 | 登录与查看申请 | 个人连接、任务、终端及工具 | 管理后台 |
|---|---|---|---|
| 邮箱待验证 | 验证和恢复流程 | 否 | 否 |
| 已验证，未申请/待审批 | 是 | 否 | 否 |
| 已验证，批准 | 是 | 是，仍检查资源所有权 | 否 |
| 已验证，拒绝/撤销 | 是，可见原因与允许的后续操作 | 否 | 否 |
| 已验证，批准并显式授予管理权限 | 是 | 是 | 是 |
| 账号停用 | 拒绝新登录；撤销现有会话 | 否 | 否 |

申请记录与账号状态分开：不能原样复制来源中的“`pending_approval` 账号不能登录”。
首版采用以下实现规则：待审批时重复提交保持原申请；拒绝/撤销后允许用户明确重新申请；
停用清除管理员身份与已获准状态，恢复账号本身不会恢复它们。撤销保留记录、阻止新工作并停止活动执行；
重新批准不自动重放积压任务。审批变更使用版本检查，防止过时页面覆盖新决定。

## 3. 从 HybridInference 复用什么

以下路径相对来源仓库；复制时记录来源提交、保留适用的署名与许可证，并连同相关测试迁移。

| 来源 | 可复用内容 | Cloud Agent 必须适配的部分 |
|---|---|---|
| `apps/backend/serving/utils/password.py` | Argon2id 哈希和校验、现有密码规则 | 依赖声明、模块路径、测试；不同时重设计密码规则 |
| `apps/backend/serving/servers/routers/auth_routes.py`、`schemas_auth.py` | 注册、登录、验证、忘记/重置密码的行为与验证 | 拆开 `OperationalStore`、Gateway settings/audit、API role、手机号路由依赖；使用 Agent 自己的端口 |
| `apps/backend/serving/auth/browser_sessions.py`、`utils/jwt.py` | 会话生命周期、刷新/撤销的成熟逻辑与测试 | 使用独立密钥和本地账号；保留 Agent 的 HttpOnly cookie/BFF 边界，不把权限作为会话内长期授权缓存 |
| `apps/backend/serving/utils/email.py` | 邮件发送与验证/恢复模板的基础逻辑 | 独立 SMTP 设置、Cloud Agent 名称与固定站点地址；不读取 Gateway 运行配置 |
| `apps/backend/serving/utils/login_rate_limit.py`、`signup_rate_limit.py`、`auth_failure_blocklist.py` | 登录和注册滥用控制 | 本地存储、可信来源 IP 处理、通知配置与审计 |
| `apps/backend/serving/auth/signup_policy.py` | 注册开关、域名规则的实现经验 | 注册资格与 Agent 准入分开；首版 Agent 默认人工批准，不能继承来源的“空白名单自动批准”语义 |
| `apps/backend/serving/servers/routers/admin/users.py` | 用户列表、状态修改、审计模式 | 只保留账号和 Agent 准入管理，排除 API key、计费、模型角色和 Gateway 运维功能 |
| `apps/frontend/src/app/{login,signup,verify-email,forgot-password,reset-password}/`、`components/auth/` | 表单、验证、加载与错误体验及测试 | 接入 Agent API、页面布局、i18n、cookie 会话；不依赖 Gateway dashboard/provider 状态 |

不整体搬入 `serving.servers.auth`、`PostgresOperationalStore`、推理配额、Gateway JWT/密钥、
模型角色和服务间 SSO。现有 Cloud Agent session cookie 的跨站保护也不能因复制旧页面而丢失。
用户设置中的邮箱来源 `user_profile.py` / `gateway.py` 要改为本地账号资料；不能在登录独立后仍访问 Gateway。

## 4. 两条线共享的最小契约

### 身份和授权

- `user_id` 是稳定、不透明的 Agent 资源所有者标识；模型连接不依赖邮箱、role、登录方式或 Gateway issuer。
- 登录提供当前身份；`AgentAdmission.decision(user_id)` 提供当前 `allowed` 与
  `permissions`，复用 `AgentAccessDecision` 的一致性规则。
- `agent.admin` 必须包含 `agent.use`。申请页面只需要有效身份，不需要 `agent.use`。
- 个人连接、任务及其他受保护接口需要当前 `agent.use` 和资源 owner 检查。
- 任务领取、自动继续、排队消息、恢复常驻进程时也检查准入，不能只在 HTTP 创建入口检查。
- 显式拒绝与服务不可用区分；不可用不能回退到 cookie 权限、旧 Gateway role 或“允许所有”。

### 前端稳定接口

继续提供 `/v1/session/me` 的 `id` 与 `agent_access`，增加独立的申请状态字段供 waiting list 页面显示。
BYOK 页面只消费这份身份/准入结果，不关心认证由旧 Gateway 还是本地账号提供。
现有 `require_agent_owner` 与管理依赖保持调用方签名，内部转为注入准入端口。

另提供明确的账户资料读取端口，Profile 与团队成员邮箱都读取本地资料。
`application/teams.py` 的邮箱和准入、`api/service_jobs.py` 的 on-call 服务账号也属于账号线的迁移范围；
内部 dispatch 凭据仅证明调用服务身份，仍需检查被配置服务账号的本地准入。

### 模型与限额

账号线不持有上游 API key，不决定 BYOK 的模型目录，不通过 Gateway role 限制个人连接。
模型连接线不能用“key 可用”代替平台准入。平台沙箱/并发限制与上游推理限额分开。
本次不引入单任务美元预算，也不自动复制 HybridInference 的额度为 Cloud Agent 额度。

过渡期的旧 Gateway 推理路径需要显式 `GatewayInferenceBinding(account_id, issuer, subject)`，
并继续由 Gateway 按其当前授权判断；历史身份映射本身不授予推理权限。
纯本地新账号没有这个绑定时必须先配置个人连接，不能把本地 `user_id` 当作 Gateway subject。
独立部署默认不开放旧 grant 模式；如用户把 HybridInference 作为普通兼容 provider，使用个人 key 连接。

### 访问终止与账号删除

A0 同时定义 `terminate_owner_access(user_id, reason, generation)` 的应用层契约：
先原子提交准入撤销/版本变化，再拒绝新 claim，按 lease fence 取消活动 attempt、释放 residency，
通过现有 `application/residency.py` 的关闭路径停止成员进程；主机失败必须有持久重试与心跳收敛。
仅删除 cookie、只取消一个 HTTP 请求、或只调用 Gateway revoke 都不算完成。
普通暂停保留加密连接配置；账号删除另行撤销连接并按 BYOK 设计处理密文销毁与未完成扫描。
身份线拥有删除/终止的协调流程，BYOK 线实现连接撤销和扫描材料清理的 hook。

## 5. 本地数据与会话

按能力拆出账户、登录凭据/身份、会话、申请/准入、管理审计，统一由 Agent DB 持久化。
迁移 `0107_local_accounts`（接在 `0106_user_model_connections` 后）建立独立 namespace、账号、密码登录、准入、会话、一次性令牌、
请求频率计数和撤权清理记录；字段由迁移和契约测试固定。

- 账户：稳定 ID、账号状态、创建/更新时间；显示资料继续由现有 Profile 能力管理。
- 登录身份：账号 ID、登录类型、唯一规范化标识、验证状态；密码只存哈希。
- 申请：账号 ID、状态、用途、申请/审核时间、审核人及公开原因；内部备注不能误回传用户。
- 准入：账号 ID、显式权限及版本。审核批准和权限写入应在同一事务生效。
- 会话：可撤销、有限有效期；密码重置和账号停用撤销旧会话。令牌、验证码和重置材料不写日志。
- 验证与恢复：有有效期、单次使用、重放保护与请求频率限制；响应不泄漏账号存在性。
- 第一位管理员由部署持有的基础设施授权显式创建/授予；公共注册不能抢占管理员。

会话采用 30 天固定有效期的随机不透明令牌，服务端只保存用途隔离的 keyed hash；
邮件验证链接有效 24 小时、密码重置链接有效 1 小时，令牌放在 URL fragment，页面显式 POST 才消费。
密码规则与 Argon2 参数适配自 HybridInference；不引入共享 JWT 或刷新令牌。
本地账号模式由进程环境 `AGENT_AUTH_MODE=local` 显式启用，默认仍是 Gateway 模式。

认证材料不能进入 host claim、sandbox、个人模型连接或公开 API 响应。
Cookie 继续遵守 `__Host-`、Secure、HttpOnly 和现有跨站写入检查。
邮件相关配置由 Cloud Agent 自己拥有；本仓库的 `workers/notifications.py` 是 Postgres 通知，不是现成邮件发送器。

## 6. 历史身份与切换

现有数据库通过 `agent_identity_namespace` 绑定一个 Gateway issuer。
不能通过修改 `GATEWAY_ISSUER` 或删除绑定来“切换”到本地身份。

推荐迁移方向：保留既有资源的 `user_id`，建立显式的
`(legacy_issuer, legacy_subject) -> local_account_id` 映射，并把旧 namespace 保留为迁移来源记录。
已有 owner ID 的继承必须与原绑定来源一起验证；新注册账号使用新的不透明 ID。
生成与插入时防止和已有/已保留 owner ID 冲突；来源由明确的身份记录识别，不靠解析 ID 字符串推断。

A1/A2 可在明确启用本地认证的隔离新数据库开发与验收；在已有 owner 数据的数据库上，
必须先完成 A3 的身份迁移、session 版本和启动检查，才能开放本地注册。
不得因 A1 先合入就对旧库开放注册或把旧 cookie 的 `gateway_issuer` 省略掉。

- 不按邮箱相等自动合并账号、继承任务、MCP/SSH 凭据或个人模型连接。
- 老用户迁移需证明原账号控制权并建立本地登录凭据；已有同邮箱本地账号的冲突走显式恢复流程。
- 不默认导入密码哈希、刷新 token 或完整 Gateway 用户表。批量导入属于另行审查的迁移方案。
- 既有准入与管理员权限按实际生效结果形成迁移清单，经核对后写入，不能把所有 active Gateway 用户视为 Agent 用户。
- 双入口过渡期必须显式映射至同一 owner；不能出现旧路由仍能绕过本地停用/撤销的情况。
- 新会话版本与旧 issuer 会话明确区分；切换时要求重新登录，不让旧 cookie 被当成本地凭据。
- 修改启动时 namespace 检查必须伴随专门迁移；不能提供忽略冲突的开关。
- 保存迁移前备份和映射清单。创建本地账号后，旧二进制不能作为无条件回滚路径。

正式切换前必须核对所有 owner 表，包括任务、团队、环境、Git/MCP/SSH、个人资料和个人模型连接。
这份设计没有读取或修改生产数据库。

## 7. 并行工作拆分

| 工作项 | 主要范围 | 与 BYOK 的依赖 |
|---|---|---|
| A0 身份与准入端口 | `api/deps.py`、`_owner_auth.py`、`owner_access.py`、资料读取和 bootstrap 装配；定义访问终止、旧推理绑定与上线门槛；保留 Gateway 适配器行为 | 两条线共同依赖的最小集成面，先审查合入 |
| A1 本地账号与会话 | 新 `auth/`、ports/repositories、迁移；注册、登录、验证、恢复及对应页面 | 可与 BYOK 数据/UI 并行，不改模型连接结构 |
| A2 waiting list 与管理 | 申请状态、审核、通知、审计及用户/管理员页面 | 只提供当前准入，不读取个人密钥 |
| A3 迁移与切换 | namespace、旧身份映射、重登、启动/回滚检查 | BYOK 仅以稳定 owner ID 参与迁移核对 |
| 集成验收 | 本地账号 + 本地准入 + BYOK 完整任务链 | 两条线单独通过后再执行 |

数据库迁移编号在合入时统一分配，避免两条分支抢同一个 revision/head。
共享的 session、用户 ID 和准入端口只由 A0 的一个变更修改；双方用接口测试协作。
前端分别新增认证/申请页面和个人模型连接页面；导航与 model picker 的最终集成集中审查。

Gateway 专属的请求明细、费用页、旧模型改写属于 BYOK/集成线：无旧绑定时不发 Gateway 请求，
按能力显示外部用量或明确的不可用状态。不能让默认后台页面继续请求不存在的 Gateway。
这不承诺把历史 Gateway 计费明细复制进本地账本。

## 8. 验收

- 全新用户不访问 HybridInference 注册/登录即可申请，获批后使用 BYOK 执行任务并续聊。
- pending/rejected/revoked 用户能查看申请状态，不能调用连接、任务、终端、MCP 或其他受保护操作。
- 普通获批用户没有管理权限；后台角色变更对旧会话、队列和自动轮次同样生效。
- 邮箱验证、找回密码、重复验证、重放、错误密码、会话撤销、跨站写入均有移植或新增回归测试。
- 停用/撤销同时覆盖正在运行和后台工作；实现报告明确停止语义及测试的时间上限。
- 用户 A 不能访问 B 的任务、连接和密钥；邮箱碰撞、伪造 legacy subject 不转移所有权。
- 切断 Gateway 的身份、准入、用户资料、模型目录、grant 与配额访问后，本地账号 + BYOK 仍可完成全链路。
- 团队成员资料、服务账号创建任务、管理页面在独立部署也不隐式读取 Gateway；不提供的 Gateway 明细能力明确标注。
- 密钥管理、Git/MCP/SSH 等各自的外部服务依赖不被误称为 HybridInference 依赖。
- `make check`；涉及 storage/SQL/migration 时 `make test-db-local`；前端变更 `make ci-frontend`。
  更新维护指南时补充中英翻译和 `make docs-verify`。

## 9. 首版边界与后续集成

本分支实现 A1/A2 及其必需的身份装配、namespace 防护和撤权路径。
local 模式只允许新的独立数据库；遇到已绑定 Gateway 或已有 owner 数据的数据库时拒绝启动，
不提供忽略冲突的开关。A3 的旧账号领取、历史数据迁移和线上切换另行审查。

账号流程可在完全不调用 Gateway 的条件下注册、验证、申请、审批及管理资源权限。
本分支基于个人模型连接 PR #1029 整合：获批用户先验证并保存自己的连接，再执行任务。
没有可用连接时明确引导配置，不会将本地账号 ID 发送给 Gateway，也不会隐式使用平台 key。
`AGENT_MODEL_CONNECTIONS_ENABLED` 在 local 模式仍是显式开关；关闭时不提供配置入口或接纳新轮次。
任务、续聊、队列、goal 和 Fork 保留 `(model, model_connection_id)`，连接失效不自动换源。
原独立账号验收记录保留为历史结果；当前整合验收单独记录了实际版本与机器。
模拟上游协议、真实账号和数据库的测试不能代替真实 provider 与 Kata host 的全链路验收。
