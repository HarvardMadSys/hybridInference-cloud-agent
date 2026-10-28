# Cloud Agent 设置管理与部署操作设计

日期：2026-10-28

基线：`dev@3f60b6afbb86c67422b3c237c3e9ab68ed2effac`

状态：Codex 与 Claude Code 三轮讨论后的设计；实现和验收分别记录，不把设计当作已上线能力。

## 1. 目标与用户要求

管理员看到 Deployment setup 后，不知道这些项目是否缺失、能否修改、在哪里修改，
也不知道保存后是否已生效。用户明确纠正：不要把当前页面没有入口解释成技术上不能改。
本设计把每项配置导向实际管理入口或明确的操作流程，并参考 HybridInference 最近的
Configuration / Settings 改动，拆成可独立审查的小任务交由 DSH 执行。

“可编辑”“通常多久修改”“如何生效”“配置是否齐全”是四个独立概念。
Advanced 是技术复杂程度，不能用来代替“通常只设置一次”。

### 参考基线

- HybridInference #1681：按主题合并设置、保留草稿、缺项和未保存标记。
- #1683：常用优先，一次性接入参数后置。
- #1656：普通字段原地编辑，保存仍审核差异。
- #1679：保存版本与进程应用版本分开，过期心跳不能算成功。
- #1652（模型/路由目录入库）在调研时仍未合并，不当成已交付能力。
- Cloud Agent 已有加密设置、首次环境导入、5 秒同步、整份快照切换、CAS、审计、
  搜索、跨标签保留草稿以及 Edit / Review / Apply。这些机制继续复用。

## 2. 决策及讨论结论

1. 七项部署说明不是七项待办，也不是七项永久禁止修改的设置。
2. 已有编辑入口必须直接链接；不能再写含糊的“在上方设置”。
3. “已配置”只代表有配置证据，不代表目标可达、凭据有效、宿主机合规或全进程已生效。
4. 不伪造尚未实现的轮换、迁库或重启按钮。当前只能文档指导的操作明确写“查看操作说明”。
5. 根密钥与会话签名密钥分开说明，特别显示根密钥仍使用旧环境变量回退的情况。
6. 同意最终提供更完整的可管理配置；分阶段交付不等于将 Token、轮换和迁库从目标中删除。
7. 第一组实现先解决管理入口和诚实的配置状态，下一组补充生效说明。鉴权变化、数据轮换、
   数据库迁移及进程确认分别实施，避免用一组 UI 改动顺带改变系统权限与数据格式。
8. Claude 初稿中“全部启动前配置”“Advanced 已覆盖一次性设置”两项判断已经撤回。
   Codex 接受其建议：不能因为 HybridInference 可以编辑 ADMIN_TOKEN，就直接把
   Cloud Agent 的恢复凭据搬入普通设置行；需先定义恢复与日常管理凭据的独立语义。

## 3. 配置管理矩阵

| 项目 | 当前来源/行为 | 用户操作 | 第一组交付 | 后续受控操作 |
|---|---|---|---|---|
| 数据库连接 | 部署环境，必须先连接才能读取保存设置 | 部署变更/迁库 | 解释依赖，操作文档入口；不返回 DSN | 预检、停写、迁移、切换、验证及回退 |
| 设置加密根密钥 | 专用环境 key，或环境 AGENT_SESSION_SECRET 回退 | 受控密钥轮换 | 显示专用/回退/缺失，不返回值或指纹 | 所有根密钥数据重加密、多进程切换 |
| ADMIN_TOKEN | 每请求读部署环境；独立于 Gateway 的完整管理员入口 | 当前部署侧修改 | 明确独立恢复入口、可选；不暗示可从 DB 修复 DB 故障 | 独立的日常管理凭据与恢复机制设计 |
| GitHub App key | 界面保存的内联私钥，或部署挂载文件 | 编辑内联私钥/更换挂载 | 链接 github_app_private_key；说明两种方式 | 单独的密钥轮换操作可复用供应商验证 |
| Compute profile | 管理员注册 profile，或部署文件 profile | 专用 Compute setup | 链接 Servers 的现有 Compute setup；说明双来源 | 来源/同名覆盖可视化，独立版本管理 |
| pgAdmin | 首次配置的私有卷、允许服务；后续 URL 已可编辑 | Admin Database / Database tools | 提供两个真实入口，解释允许列表 | 若扩大可管理服务范围，单独设计 SSRF 边界 |
| 基础设施 | 监听/挂载/网络/实际隔离运行时由部署管理；部分应用策略已可编辑 | 依项目进入设置/部署工具 | 删除泛称所有 proxy trust 都不可改的描述 | 逐类受控操作；不提供通用 shell/任意路径编辑器 |

### 与 HybridInference 的准确比较

HybridInference 的 ADMIN_TOKEN 和 JWT_SECRET_KEY 是可编辑设置；修改 JWT 会使既有登录失效。
它的 DB_HOST/DB_PORT 等仍属于数据库启动依赖；API_KEY_SECRET/ERASURE_FENCE_SECRET
设置后锁定，因为既有记录依赖它们。Cloud Agent 的 session_signing_secret 已可编辑；
source_control_encryption_key 与 compute_encryption_key 则是已有数据的解密材料。
“安全”不是禁止所有编辑的理由；必须说明具体数据依赖和实现所需流程。

## 4. 第一组实现：可发现、可操作、状态诚实

### 4.1 管理入口元数据

保持现有 `bootstrap` 字段及 `key/label/description` 不变，增加可选字段，兼容旧前端：

```typescript
type DeploymentManagement = 'settings' | 'specialized' | 'rotation' | 'deployment';
type DeploymentRequirement = 'required' | 'optional' | 'conditional';
type DeploymentState = 'configured' | 'missing' | 'weak' | 'fallback' | 'not_used' | 'unknown';
interface DeploymentSetupDefinition {
  id?: string; // Stable semantic ID; keep legacy key-based anchors as aliases.
  key: string;
  label: string;
  description: string;
  management?: DeploymentManagement;
  requirement?: DeploymentRequirement;
  state?: DeploymentState;
  // Fixed application-owned destinations, never arbitrary backend-provided URLs.
  actions?: { target: string; label: string }[];
}
```

后端只输出固定 target，例如 `github_app_private_key`、`compute_setup`、`database`、
`pgadmin_url`、`deployment_guide`、`key_rotation_guide`。
前端用白名单映射为应用路由、字段定位或固定项目文档链接；未知 target 不渲染为可点击链接。
没有真实迁移工具时，rotation 对应文档动作，不能渲染成“立即轮换”。

`state` 不返回 secret、连接串、实际目录、密钥散列或外部异常信息。
根密钥长度由已有校验判断；恢复 token 只能说明配置存在。
私钥存在性检查复用现有逻辑，不加载私钥、不主动连接外部系统。
pgAdmin 的 PUBLIC_URL 有合法回退，不能用“三个变量没填齐”判定缺失；
仅配置 BOOTSTRAP_URL/DIR 的检查也要复用实际默认语义，不能从 bootstrap 层导入 HTTP router。
Compute/基础设施无法可靠判断时使用 unknown，不将未探测标为错误或健康。
可选未启用功能使用 not_used，不计入“缺少必需配置”。
数据库 GET 成功只能证明当前控制面成功读到了配置，不能证明所有副本或灾备库健康。

后端新增窄模块承载部署说明，避免扩大接近 1500 行的 settings.py。
不改变环境导入、secret 加密、revision、存储 schema、鉴权和审核逻辑。

### 4.2 前端入口

- Admin Settings 增加末尾 Deployment 标签，部署说明在设置页只挂载一份。
- /setup 恢复页面继续可见；其 recovery token 保持仅内存保存与 credentials: omit。
- 全局搜索定位到 Deployment；保留 `application-bootstrap-${key}` 旧元素 ID。
- 原有深链接、搜索焦点、键盘 tab 导航和草稿生命周期必须保留。
- 点击具体设置入口时导航到正确 category 并定位目标字段。
- Compute 入口需要确认实际页面/锚点；没有可定位锚点就补一个，不制造无效链接。
- 旧后端没有新字段时展示旧说明；只在响应提供动作元数据时显示管理入口，
  不崩溃、不推断健康，也不从旧说明文本猜测可执行操作。
- 长变量名在窄屏换行；状态文案用“配置存在”等词，避免“运行正常”。

### 4.3 生效说明

设置定义新增可选 `apply_mode` 和 `apply_description`。
模式：`next_operation`、`new_session`、`restart`、`host_reenroll`、`unverified`。
动态锁定仍复用 read_only；轮换要求通过独立说明表达，避免两个字段矛盾。
字段必须按真实读取点逐项标记；不能把默认描述当成已验证热更新能力。
未核实项默认 unverified；已有动态读取项在验证读取方后标 next_operation。
明确快照同步不等于所有进程已确认。
已知 MCP 私有地址/明文 HTTP 策略为指定 listener restart；默认执行策略为新会话。
凭据加密 key 的锁定依赖当前值，不能因为初次可设置就显示可直接替换。

审核弹窗逐项显示作用范围，保存反馈只承诺保存成功。
本阶段没有 instance heartbeat，不能出现“全部进程已生效”的指示灯。

## 5. 后续 UI 组织

- 独立表达 routine/setup 修改频率，不替换现有 important/advanced 技术层次。
- Common 默认页只放经过选择的日常控制；每个字段只存在一个编辑器，避免两份草稿。
- 旧 category 和字段 hash 有明确兼容映射；未知新字段仍有展示位置。
- 按功能 setup groups 汇总各 tab 的缺项时对 key 去重；可选尚未启用的不计入。
- 未保存标记覆盖全部编辑器；409、切 tab、后台轮询不会丢草稿。
- 普通字段可以原地编辑，仍通过 Review + CAS；secret 替换/清除保留显式动作。
- 先实现入口和生命周期元数据，再做重组，方便定位回归。

## 6. ADMIN_TOKEN 可管理化：独立设计阶段

当前 ADMIN_TOKEN 通过 verify_admin_access 授予完整管理员权限，影响管理路由、owner
参数例外、on-call 管理兜底和审计 principal；不是仅仅给 /setup 使用的一把钥匙。

不将其直接注册成普通设置并自动导入，否则“清空”可能被环境回退恢复，或在数据库/解密
故障时丢失恢复入口。下一阶段评审两种具体方案：

A. 部署恢复 token 独立保留，增加 DB 管理的日常 API credential；默认关闭，不自动复制。
凭据仅创建时展示，数据库保存单向 verifier，撤销/替换必须有多进程失效策略；区分审计身份。
B. 单 token 入库并定义外部恢复介质、导入/清空/回退规则；必须先完成故障恢复工具再迁移。

优先研究 A；不得未经审查把两个 token 简单 OR 到所有 verify_admin_access 路径。
明确哪些路由接受日常凭据、哪些要求当前 Gateway admin 或部署恢复身份，以及角色撤销含义。
验收包含：新 token 默认无效、撤销无陈旧缓存、旧恢复入口保持、数据库故障行为、审计无泄密、
on-call scoped token 不升级为管理员，以及旧版本回退不会意外重新开放已撤销入口。

## 7. 密钥轮换与迁库：可实现，但必须完整

### 7.1 当前密钥依赖

| 材料 | 需要枚举并验证的数据 |
|---|---|
| settings root | agent_settings 的 secret 字段；runner policy 的归档凭据 |
| user credential key | GitHub/GitLab 连接；MCP 连接/调用/OAuth；环境变量及依赖密钥的 MAC/指纹；SSH 私钥 |
| compute key | compute connections、注册 deployment profile 的环境和文件 |

特别注意：root 回退读取部署环境 AGENT_SESSION_SECRET；saved session_signing_secret
是另一个可编辑值。不能将后者的新值同步回环境而假定只是改变登录签名。
本清单是设计入口，实施前必须以各表/列/密钥域的可执行 inventory 校验覆盖，不凭手工列表执行。

### 7.2 轮换状态机

计划（枚举/备份/校验）→准备新 key ID→阻止或协调并发写→重加密并校验→发布新版本→
等待全部参与者确认→验证重启和读取→退休旧 key。
持久化 operation ID、源/目标 key ID、阶段和批次游标；重试幂等；不在审计或日志保存 key。
每阶段明确失败重入与回滚边界，测试进程崩溃、跨版本进程、双写、部分失败、备份恢复。
在这些机制未实现前只提供操作说明，不提供调用空壳。

### 7.3 数据库迁移

数据库地址需要服务启动时从独立来源读取，但可以由部署工具受控修改。
预检目标 schema/扩展/身份 namespace →停写与排空→一致性导出→导入/校验→更新部署连接→
逐实例切换/健康检查→开放写入。恢复写入后不得简单切回旧库造成分叉。
操作必须由拥有部署权限的执行器实施；普通 settings API 不接受任意 DSN 并直接拨号。

## 8. 进程应用状态阶段

沿用现有保存 revision；主 API/MCP listener 各自上报实例身份、构建版本、已读取版本、
组件已应用版本、心跳和 restart-required 项。快照已读取不等于构造于启动时的客户端已换配置。
按部署 inventory 判断覆盖；过期、遗漏、未知能力、恢复后的 revision 历史都不能算已应用。
仅有 current revision 不足以证明原子资源切换；需要重建组件时再引入 prepare/publish/dispose。

## 9. 安全、兼容与验收

第一组实现不改变 DB schema、鉴权、凭据有效期、网络规则、默认隔离策略或生产配置。
任何后续阶段的权限/轮换操作有自己的验收，不因本设计获得生产执行授权。

- API：普通用户拒绝；只输出固定元数据；新旧响应兼容；secret 字段仍为 null。
- 前端：真实入口可达，旧 hash/搜索可定位，跨 tab 草稿不丢，409 不覆盖别人更新。
- 配置：专用 root/fallback/missing 区分，optional 不等于 missing，unknown 不等于 healthy。
- 运行时：保存提示不承诺未验证的热更新；restart 项明确。
- Node 22 `make ci-frontend`；后端 `make check`；涉及 DB 再跑数据库 tier。
- 修改维护文档时同步翻译并 `make docs-verify`。
- 自测与独立验收分别记录实际机器和源码版本；浏览器 fixture 不冒充真实部署验收。

## 10. 实施记录

细分步骤及 DSH 提示词见同目录 `2026-10-28-settings-management-dsh-steps.zh-CN.md`。
实现进度、审查意见、未通过的检查记录在 reviews，不回写本设计为“已完成”。

### 第二轮审查的取舍

接受 Claude 的稳定 ID、unverified 默认生效模式和根密钥回退提示建议。
不采纳其“新增 AST 扫描所有 DOMAIN 常量”测试建议：源文本扫描既不能证明全部数据
覆盖，也会与重构耦合；轮换前用真实数据家族的加解密/往返与故障测试验证。
不将其 pgAdmin“三个变量缺一就是 missing”建议照搬：PUBLIC_URL 存在合法默认。
Common 与原地编辑保留为后续目标，不因为现有 Edit 流程刚合并就永久拒绝改善。
所有后续阶段都有任务卡；未实现时明确标记 planned，不能按首组交付算全目标完成。

### 第三轮评审：实施前关闭的问题

1. `/setup` 的 recovery token 仅存在页内存。恢复上下文只允许本页字段定位与固定文档
   （新窗口且不传 token）；Compute/Database 等跨页面动作改成“Gateway 登录可用后前往…”
   的纯文字，不跳转、不把 token 塞进 URL/storage。Admin Settings 内只挂一份；恢复页保留自己的说明。
2. 每一项状态计算失败隔离为 unknown，不能使 settings GET 失败；不调用 pgadmin_config
   router、不重新加载 Compute 文件。状态只描述本进程已有事实或明确未检查，不用磁盘内容
   冒充启动后已采用配置。测试坏 Compute 文件/pgAdmin 地址仍能打开编辑器。
3. fallback 切到 dedicated：专用 key 使用当前环境 fallback 的完全相同字节，可以保留
   密文兼容；设新随机 key 则是真正轮换，必须重加密。即使是同值分离也需协调所有进程并验证
   设置及 runner policy 归档凭据可读。只有专用 key 在所有进程生效后，旧环境签名材料才可独立调整。
   测试同值解密成功/不同值失败，不在 UI 提示“随便新生成一把专用 key”。
4. 数据库连接项链接部署操作文档，不能链接 Admin Database 并让人误以为该页会迁库。
   根 key 文案只说“此 key 保护的密文”，不说整个数据库所有数据都需重加密。
