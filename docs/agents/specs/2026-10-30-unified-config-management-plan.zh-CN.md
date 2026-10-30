# 统一前端配置管理：分阶段实施计划

- 日期：2026-10-30
- 状态：**计划，未实现**。本文没有任何一项已经交付；各工作包的实现和验收另行记录在 `docs/agents/reviews/`。
- 依据：[P1 第二次修订](2026-10-29-env-inventory-and-gates.zh-CN.md)（两轴映射 §7、边界 §8、Admin 体验 §9）；
  [设置管理设计](2026-10-28-settings-management-design.zh-CN.md) 及其 [执行卡](2026-10-28-settings-management-dsh-steps.zh-CN.md)
  （S01–S12 已实现，S13 起未实现）；[发布说明](../../../deploy/release/README.md)；
  [备份与恢复](../../developer/disaster-recovery.md)。
- 基线：分支 `codex/config/env-inventory-gates@322a8fd0`（`dev@4b7b826d` 加盘点与门禁）。

## 1. 目标与原则

目标是让尽可能多的配置能在 Admin 里管理，同时不放宽沙箱、网络和凭据边界。落实方式：

1. **按语义项交付，不按变量交付**。一个工作包覆盖一个模块边界内的一组语义项；不为每个变量开 PR。
2. **普通交付不等最难的项目**。阶段 1、2 只用现有机制（设置行、心跳、实例上报），不依赖部署代理。
3. **状态如实**。每个工作包交付时，前端只显示它真正有证据的状态；没有证据就是「未知」。
4. **期望和实际分开**。操作员选择期望值（设置 revision、配置版本、期望发布）；实际值只来自进程、主机或代理的上报。
5. **引导值不进它所引导的存储**。数据库连接、根密钥、`ADMIN_TOKEN` 的权威副本在部署代理的存储里。
6. **不提供任意执行**。部署代理只执行封闭的操作集合，参数有类型，边界取值受本机策略约束。

## 2. 工作包总览与依赖

| 阶段 | 工作包 | 归属模块 | 依赖 | 覆盖的族（P1 §7） |
|---|---|---|---|---|
| 1 | WP1 普通设置与导入欠账 | `cloud_agent/settings.py`、`api/admin_settings.py`、`app.py`、ENVIRONMENT.md | 无 | F07、F14、F18 |
| 1 | WP2 主机设置扩展 | `cloud_agent_shared/host_settings.py`、`cloud_agent_host/runner.py`、`api/admin_hosts.py`、`runtime_upgrades.py` | 无 | F05（主机日志）、F11、F15、F16 |
| 1 | WP3 管理目录与生效元数据 | 新 `cloud_agent/management_catalog.py`、`setting_definition.py`、`deployment_setup.py` | 无 | 全部可管理的族的元数据 |
| 2 | WP4 控制面实例上报 | 新 `cloud_agent/instances.py`、存储与迁移、`settings.py` 同步循环 | WP3（词汇） | F04、F17、F22（控制面部分） |
| 2 | WP5 统一前端 | `apps/frontend` 的 `admin/settings`、`admin/hosts`、新 `admin/deployment` | WP3；状态部分依赖 WP4、WP6 | 全部 |
| 3 | WP6 部署代理与引导存储 | 新 `deploy/agent/`（随发布配方）、`application/deployments.py`、`api/deploy_agents.py`、`api/admin_deployments.py` | WP3 | F22、F31（观测与参数）、F40–F41 |
| 3 | WP7 部署配置的受控应用 | `deploy/agent/`、`deploy/release/release_env.py`、`application/compute_setup.py` | WP6 | F20、F21、F24、F25、F27、F28、F29、`COMPOSE_PROFILES` |
| 3 | WP8 发布、镜像与 Kata 操作 | `deploy/agent/`、`deploy/*/deploy.sh`、`deploy_readiness.py`、`setup_kata_runtime.sh` | WP6、WP4 | F22、F25、F30、F31、F32 |
| 4 | WP9 管理员凭据 | `api/deps.py`、新凭据存储、Admin 凭据页 | 日常凭据：无；`ADMIN_TOKEN` 轮换：WP6 | F19（`ADMIN_TOKEN`） |
| 4 | WP10 密钥轮换 | 新 `application/key_rotation.py`、`domain_cipher.py`、`credential_cipher.py`、心跳 | WP4；根密钥和主机侧再加 WP6 | F02、F10（manager 令牌）、F26（重新加入）、F27（隧道密钥）、F33 |
| 4 | WP11 同库连接更新 | `deploy/agent/`、`app.py` lifespan、`deploy_readiness.py` | WP6、WP4 | F19（`AGENT_DATABASE_URL`）、F24（`AGENT_POSTGRES_*`） |
| 4 | WP12 换库迁移 | `deploy/agent/`、迁移工具、恢复手册 | WP11 | F19 |

```text
WP1 ─┐
WP2 ─┼─► WP5a（目录与生效说明上线）
WP3 ─┼─► WP4 ─► WP5b（已保存 / 已采纳）
     │           └─► 模型连接开关（随 WP4）、WP10（数据库内的密钥）
     └─► WP6 ─► WP5c（只读「部署」区）─► WP7 ─► WP8
                 ├─► WP9 的 ADMIN_TOKEN 轮换
                 ├─► WP10 的根密钥与主机侧
                 └─► WP11 ─► WP12
```

工作包的大小：每个工作包 1–3 个 PR，按模块边界拆，例如 WP2 拆成「字符串型主机设置 + 注册表」和「租期 + 能力欠账」两个 PR。

## 3. 可用的纵向切片

| 切片 | 组成 | 用户能做什么 |
|---|---|---|
| VS1 | WP1 + WP3 + WP5a | 每个设置组显示来源、管理方式和生效方式；事件流上限、API 文档可在 Admin 改；网页搜索的导入欠账修好 |
| VS2 | WP2 + WP4 + WP5b | 设置保存后看到哪些控制面实例、哪些主机已采纳；runner 租期和运行时注册表可改 |
| VS3 | WP6 第一部分 + WP5c | 「部署」区只读：每台接入代理的机器的实际发布、已应用配置版本、最近操作、残留旧名 |
| VS4 | WP7 第一项 | 一个低风险的应用项端到端：性能剖析（F29），改值 → 代理渲染 → 重启 → 报告 |
| VS5 | WP8 | 选择期望发布，代理按阶段部署主机和控制面，前端显示每一步的证据 |
| VS6 | WP9–WP12 | 管理员凭据、轮换和数据库操作，各自独立交付 |

VS1 和 VS2 不依赖部署代理，可以先上线。

## 4. 阶段 1：现有机制

### WP1 普通设置与导入欠账

**范围**

1. `AGENT_MAX_STREAMS_PER_USER` 注册为整数设置并进 `MIGRATED_SETTINGS`；`max_streams_per_user()` 读设置。
2. 导入欠账：P1 §5.1 的 8 个名字（四个网页搜索变量、两个 MCP GitHub OAuth 变量、`AGENT_WEB_DOCS_URL`、
   `AGENT_WEB_CONTACT_URL`）移进 `MIGRATED_SETTINGS`，同时从门禁的 `UNMIGRATED_ENV_SETTINGS` 冻结表删除。
3. `AGENT_API_DOCS` 注册为布尔设置；文档路由总是注册，请求时按快照决定返回内容或 404。
4. ENVIRONMENT.md 补网页搜索四行，写明「首次导入，之后在 Admin 修改」；同步中文翻译（它是文档站点的 configuration 页）。

**输入/输出契约**：设置定义沿用 `SETTINGS` 的现有结构；`GET/PUT /admin/settings` 的形状不变，只多出新键。

**升级与导入**：已有数据库下次启动时，只对设置行里**缺失**的键导入一次环境值；管理员保存过的值不受影响。
这意味着一个曾经在环境里设置、却从未被导入的旧值，会在升级时第一次生效——要写进升级说明（`docs/developer/upgrade-notes.md`
及翻译）。导入遇到非法值会抛 `ValueError` 让控制面起不来（P1 §5.1），所以新设置的校验范围必须包住所有可能的旧值。

**失败与回滚**：回滚到旧版本时旧代码忽略多出的键，设置行不需要回退。

**验收**

- 已有数据库加 `AGENT_MAX_STREAMS_PER_USER=40` 升级后，数据库值为 40；Admin 改成 10，约 5 秒后第 11 个流返回 429；之后改环境变量不再生效。
- 已有数据库（缺 8 个键）加对应环境值升级后，值被导入一次；已有值的数据库不变。
- `AGENT_API_DOCS` 打开后约 5 秒内文档可访问，关闭后 404，不重启。
- 门禁（`test_env_classification.py`）通过：`candidate` 族移到 `admin`，冻结表同步缩小。
- `make check`；`make test-db-local`（导入行为涉及数据库）；`make docs-verify`。

### WP2 主机设置扩展

**范围**

1. `HostSetting` 支持字符串值（含长度和格式校验），两个镜像都携带同一份定义。
2. `AGENT_RUNTIME_REGISTRY_URL` 成为一个设置：控制面经字符串型主机设置下发；主机在心跳里回报正在使用的注册表；
   `runtime_upgrades.py` 列版本时，只在所有在线主机都回报新地址后才改用新地址。
3. `AGENT_LEASE_TTL` 进 `HOST_SETTINGS`（runner 范围，整数秒，下限 > 40 秒，上限 ≤ 900 秒）；runner 每次认领用生效值并传给回合子进程。
4. 修欠账：`runner._shared_live_host_capabilities` 改为校验生效值，而不是本地 `AGENT_SHARED_RESIDENT_CAPACITY`。
5. 可选：主机日志级别和格式作为主机设置（需要先核实主机进程能否不重启换日志配置；不能就标需重启）。

**输入/输出契约**：心跳应答里的主机设置字典新增字符串值；心跳请求新增 `runtime_registry_url`（可选字段，旧控制面忽略）。

**升级**：旧 runner 不认识新键，`admin_values` 丢弃它，继续用环境值；`settings_local` 让页面显示「仍用本地值」。
旧控制面收到新字段时忽略（字段可选）。

**失败与回滚**：注册表切换如果某台主机迟迟不回报，控制面保持旧地址并在页面上点名那台主机；改回旧值走同样顺序。

**验收**

- 新 runner 的认领请求里 `lease_ttl_seconds` 等于管理员值；旧 runner 显示仍用本地值。
- 注册表改值后，控制面在最后一台在线主机回报前继续用旧地址列版本。
- 本地 `AGENT_SHARED_RESIDENT_CAPACITY` 越界、管理员值合法时，主机仍声明 shared-live 能力。
- 门禁通过（`host_admin` 条目等于 `HOST_SETTINGS`；`HOST_BOOTSTRAP_READS` 少一条欠账）；`make check`。

### WP3 管理目录与生效元数据

对应执行卡 S13–S20，扩展到部署项。

**范围**

1. `Setting` 增加可选的 `apply_mode`（`next_operation`、`new_session`、`restart`、`host_reenroll`、`unverified`，默认 `unverified`）
   和 `apply_description`；按真实读取点逐项标注（S14–S16）。
2. 新模块 `management_catalog.py`：产品侧的语义管理目录。每一项：

   ```text
   CatalogItem {
     id: str                    # 稳定语义 ID，例如 "deployment.control_plane_listeners"
     group: str                 # 功能分组（与现有设置分组一致）
     method: edit | apply | workflow | observe | object
     sources: [{kind: settings | host_settings | deployment_store | mounted_file
                    | release | observed, reader: str, meaning: str}]
     owner: str                 # 固定的应用内目标（设置键、Servers 面板、部署区操作）
     apply: {mode, restart_scope?: [service]}
     available: bool            # 这个方式今天是否已实现；false 时前端只显示来源，不显示操作
   }
   ```

   目录是静态声明加本进程已有的事实；不读挂载文件、不拨网络。`deployment_setup.py` 的现有状态逻辑并入目录，
   旧的 bootstrap 响应保持兼容。
3. `GET /admin/management/catalog`（管理员）返回目录；不返回任何值、路径或密钥指纹。

**与门禁的关系**：分类表不变。WP3 评审时决定是否加一条测试，要求每个非「不开放」的族至少对应一个目录项（P1 §6.5）。

**验收**：普通用户 403；目录不含值；`available=false` 的项没有可点的操作；旧前端忽略新端点；
`apply_mode` 的每个非 `unverified` 标注有读取点依据（评审逐项核对）。

## 5. 阶段 2：真实状态

### WP4 控制面实例上报

对应执行卡 S46–S47。

**范围**：每个控制面进程（主 API、MCP listener、后台 worker）启动时生成实例 ID，定期写一行：

```text
ControlPlaneInstance {
  instance_id, role: api | mcp | worker, machine: str,
  build_sha, capabilities: [str],
  adopted_settings_revision: int,
  restart_required: [setting_key],   # 启动时构造、快照变了也不会重建的组件
  started_at, last_seen
}
```

数据库迁移新增一张表；过期行（例如 3 个同步周期未更新）视为离线。

**状态计算**：某个设置 revision「已采纳」= 所有在线、角色相关的实例 `adopted_settings_revision ≥ revision`，
且该设置不在它们的 `restart_required` 里。离线、未上报、不认识的实例都不算已采纳。主机部分沿用心跳。

**附带交付**：`AGENT_MODEL_CONNECTIONS_ENABLED` 注册为设置；打开时检查所有在线控制面实例都声明了模型连接能力，
否则拒绝并点名实例。混合版本安全、`application_name` 和 SQL 触发器作为这项的设计输入，单独评审。

**升级**：旧实例不上报，页面显示「未知」，不显示已生效。回滚到旧版本时表保留、不再更新，前端回到旧显示。

**验收**：两个实例中一个停在旧 revision 时页面显示「1/2 已采纳」；停掉的实例在过期后不计入；MCP 两个 ALLOW 开关在 listener
重启前显示「待重启」；`make check`、`make test-db-local`。

### WP5 统一前端

分三步，随依赖交付。延续现有决定：按功能分组、字段直接可见、不用 Advanced 折叠、每个字段只有一个编辑器、
不恢复 Deployment 说明卡片（对应执行卡 S21–S29 中仍适用的部分）。

- **WP5a**（WP3 后）：设置组显示每项的来源、管理方式和生效说明；Review 弹窗逐项显示作用范围；保存提示只说「已保存」。
- **WP5b**（WP4 后）：每项显示「已保存 → 已采纳（n/m）」；多含义项分行显示（例如会话签名和根密钥回退）；Servers 页每台主机显示已应用和仍用本地值的项。
- **WP5c**（WP6 后）：新的「部署」区。只在至少一台机器接入代理后出现；列出机器、实际发布、已应用配置版本、
  操作记录；操作按钮只出现在目录里 `available=true` 的项上。

**验收**：Node 22 `make ci-frontend`；旧后端（没有目录端点）下页面与今天一致；未知目录项有可见位置；
草稿、深链接、搜索、409 行为不回归；离线实例不显示为已生效。

## 6. 阶段 3：部署管理

### WP6 部署代理与引导存储

**为什么要代理**：自管主机和控制面机器今天没有任何执行器——部署是人或下游发行版在机器上运行 `deploy.sh`。
托管 compute 主机已有控制面的 SSH 执行器，保留不动（P1 §12 未决问题 1）。

**组件**

- `deploy/agent/`：随发布配方分发的 Python 程序（配方已经要求 Python 3.12 和 `deploy/release/requirements.txt`），
  以 systemd 服务运行在机器上，用户与今天运行 `deploy.sh` 的用户相同。
- **引导存储**：`DEPLOY_ROOT` 下新增

  ```text
  agent/
    identity.json        # 代理 ID、机器名、角色（control-plane | host）、公钥
    credential           # 代理凭据（0600）
    policy.json          # 本机策略：允许的发布锁、Kata 对、边界取值、路径根、剖析上限；只在本机修改
    config/<rev>.env     # 每个已应用的配置版本（完整 .env），保留最近 N 个
    operations/<id>.json # 操作记录：参数、阶段、结果；本机执行的操作同样记录
  ```

  现有的 `.env`、`.deployed-sha`、`.shared-live-ready` 保持原位和原含义；`.env` 始终等于当前已应用版本的副本。
- **控制面侧**：`application/deployments.py`（意图、授权、审计、状态归并）、存储表 `deploy_agents`、`deployment_intents`、
  `deployment_reports`；`api/deploy_agents.py`（代理凭据鉴权，只能读写自己的意图和报告）；`api/admin_deployments.py`（管理员）。

**协议（拉取）**

```text
代理 → 控制面  POST /deploy-agents/report
  {agent_id, observed: {deployed_sha, manifest_sha256, applied_config_rev,
   services: [{name, state, image_digest}], kata: {version, sha256}?,
   legacy_names_present: [str], unmanaged_keys: [str]},
   operations: [{operation_id, state, phase, detail, origin: intent | local}]}

代理 → 控制面  GET /deploy-agents/intents?after=<cursor>
  [{intent_id, operation, params, config_rev?, sealed_values?: {key: ciphertext},
    requested_by, requested_at, expires_at}]
```

`operation` 是封闭集合：`apply_config`、`restart_service`、`release_prepare`、`release_prepare_database`、
`release_activate`、`release_abandon`、`kata_install`、`rotation_step`、`database_switch`。代理对每个参数按本机策略再校验；
不认识的操作或参数拒绝并报告。`sealed_values` 用代理公钥加密，在浏览器里加密，控制面只存密文。

**接入与冷启动**：`agent init` 在机器上执行：读取发布配方，接管已有 `DEPLOY_ROOT`（把现有 `.env` 记为配置版本 0，
不改写任何未知键，未知键作为 `unmanaged_keys` 上报），生成密钥对。注册使用管理员在「部署」区签发的一次性代理加入令牌；
全新安装时控制面尚不存在，`agent init` 在本机完成首次部署（与今天 `deploy.sh` 相同的阶段），之后再注册。

**失去控制面**：代理继续运行；本机命令 `agent status`、`agent apply --rev <n>`、`agent release ... --manifest <已验证清单>`
在机器权限下执行并记录为 `origin: local`；控制面恢复后上报，控制面显示漂移，由管理员采纳或重新下发。代理不自动撤销本机操作。

**第一部分交付（VS3）**：只做注册、报告和只读「部署」区，没有任何操作。

**验收**

- 代理凭据不能读其他代理的意图，不能调用任何管理 API；runner 凭据不能调用代理 API。
- 不认识的 `operation`、越出策略的参数、过期意图被拒绝并上报。
- 控制面数据库和日志里搜不到 `sealed_values` 的明文。
- 断开控制面后本机命令可用；恢复后本机操作出现在操作记录里，漂移可见。
- 接管已有 `DEPLOY_ROOT` 前后 `.env` 字节不变。
- 代理与配方的脚本测试（沿用 `deploy/release` 的录制 Docker 调用方式）；`make check`；涉及新表时 `make test-db-local`。

### WP7 部署配置的受控应用

**范围**：给 WP3 目录里 `method=apply` 的项实现 `apply_config`：

1. 部署配置模式：从 `release_env.py` 的校验和各 `.env.example` 生成有类型的键集合，按语义项分组
   （控制面监听与 edge、pgAdmin 服务、web 控制面地址、性能剖析、主机拓扑、隧道、沙箱网络取值）。
   模式随发布配方分发，代理和控制面共用同一份。
2. `apply_config` 流程：生成新版本 → 校验（模式 + 本机策略）→ 写 `config/<rev>.env` → 原子替换 `.env`
   → 只重建受影响的服务（模式里每个键声明它影响的 Compose 服务）→ 健康检查 → 报告；失败时恢复上一版本并重建。
3. 托管 compute 主机：`compute_setup._assets` 改为用同一份模式渲染，经现有 SSH 执行器应用。
4. 边界取值（F28）：只能选本机策略里列出的值；受保护网段只能增加；`ALLOW_UNISOLATED`、`EXTRA_ARGS` 不在模式里。
5. 第一项（VS4）：性能剖析（F29）。之后依次：pgAdmin 服务、控制面监听、主机拓扑。
   改控制面自己的监听地址时，代理先在新地址上做健康检查，失败就恢复；页面提示可能需要用新地址重新打开。
6. 内置数据库的身份和口令、隧道 SSH 密钥不在这里，属于 WP11、WP10。

**升级**：一个语义项只有在管理员在「部署」区「接管」后才由代理管理；接管前对应的 `.env` 行只读显示。

**验收**：每个已实现的语义项有一条端到端测试（录制 Docker 调用）：成功、校验失败、健康检查失败后恢复；
越出本机策略的值在控制面和代理两侧都被拒绝；`make check`。

### WP8 发布、镜像与 Kata 操作

**范围**

1. **期望发布**：管理员从代理本机发布锁里列出的清单中选择一个（清单 SHA-256 已由下游锁固定）；
   不接受任意清单 URL。降级只允许清单的 `verified_rollback_to` 包含当前发布、且数据库修订兼容时进行，其余拒绝并说明。
2. **编排**：控制面按发布说明的顺序给各代理下发阶段——所有机器 prepare → 控制面 prepare-database → 主机 activate
   → 控制面 activate。每一步等上一步的代理报告；`deploy_readiness` 的精确提交围栏不变。失败时按发布说明执行
   `abandon` 或保留旧服务。
3. **控制面自更新**：P1 §8.5；activate 期间前端断连，重连后从代理报告恢复显示。
4. **Kata**：主机代理的 `kata_install` 只接受本机策略里的版本—摘要对（来自发布的主机配方）；先排空 runner 再安装、校验，失败保留旧运行时。
5. **镜像构建输入**（F30）只观测：从清单读运行时目录显示。Agent CLI 版本继续由现有 runtime upgrades 管理。
6. web 变体（`root` / `agents`）随期望发布一起选择；改基础路径时同一个操作里提交 Gateway 回调地址（F01）。

**验收**：在录制 Docker 的配方测试里跑完整编排：正常、主机 activate 失败、控制面就绪围栏失败、abandon；
不在锁里的清单被拒绝；`make check`。真实主机、Kata 和注册表的验证属于环境切换验收，不在本仓库测试里冒充。

## 7. 阶段 4：凭据、轮换与数据库

这四个工作包各自先产出设计并评审，再实现（对应执行卡 S30–S45 的设计门）。

### WP9 管理员凭据

- **现状**：`ADMIN_TOKEN` 在每个请求里读环境，授予完整管理员权限（所有管理路由、owner 参数例外、on-call 兜底、审计主体）。
- **日常凭据**（不依赖 WP6）：数据库管理的管理 API 凭据，默认没有；只在创建时显示一次；数据库存单向校验值；
  可撤销，多进程撤销无陈旧缓存；审计主体可区分。先列出所有 `verify_admin_access` 调用路径（S30），再决定每类路由接受哪种凭据。
- **`ADMIN_TOKEN` 轮换**（依赖 WP6）：新值以密封值交给控制面机器的代理，写进部署存储，重启 API 和 MCP listener，
  确认新值可用后旧值失效。不放进设置行，不从数据库恢复。
- **待定**：`ADMIN_TOKEN` 能否发出部署意图；它的权限是否收窄。收窄之前任何文档都不能把它称为窄范围恢复令牌。

### WP10 密钥轮换

共用一个操作日志（operation ID、源/目标密钥 ID、阶段、批次游标），每阶段可重入，所有参与者确认前旧材料不退役。

| 材料 | 流程 | 依赖 |
|---|---|---|
| 设置根密钥（F33） | ① 回退分离：专用根密钥使用当前回退的相同字节，密文不变，等所有进程确认；② 真正轮换：重加密设置行 secret 字段和 runner policy 归档凭据。新密钥只写部署存储 | WP6、WP4 |
| 控制签名密钥 | 双密钥：先让所有验证方同时接受新旧两把，再用新的签发，最后退休旧的；替代今天「替换即让主机重新加入」 | WP4、主机心跳 |
| sandbox manager 令牌 | 双令牌窗口；托管主机由控制面重新渲染 `.env`，自管主机由主机代理 | WP4；自管主机 WP6 |
| 两把凭据加密密钥 | 重加密各自的数据族；先用真实数据族的往返测试证明覆盖（S37–S43） | WP4 |
| 主机凭据与重新加入、隧道 SSH 密钥 | 签发新凭据或密钥 → 主机代理切换 → 撤销旧的 | WP6 |

**验收**：每种材料在每个阶段崩溃后重入成功；跨版本进程在窗口期内都能工作；审计和日志里没有密钥。

### WP11 同库连接更新

**是什么**：同一个数据库的地址、账号或口令变了，数据不动。包括内置 PostgreSQL 容器的口令轮换。

**流程**：管理员提交新连接参数（密封值）→ 控制面机器的代理用新参数试连并检查是同一个库（身份命名空间、迁移修订一致）
→ 写新配置版本 → 逐个重启控制面服务（API、MCP listener、worker）→ WP4 确认所有实例用新连接上报 → 完成。
内置数据库口令：先 `ALTER ROLE` 设新口令（旧连接继续可用），再按上面切换，最后确认没有旧连接。

**失败**：试连失败不写配置；切换中途失败回到上一配置版本（旧参数仍有效时）。控制面 API 不接受任意 DSN 也不拨号，
试连只在代理上做。

**失去控制面**：连接参数错了导致控制面起不来时，前端不可用；机器上 `agent apply --rev <上一版本>` 恢复。

### WP12 换库迁移

**是什么**：把数据搬到另一个数据库。与 WP11 不同，涉及数据一致性。

**流程**（S44–S45）：预检目标（版本、扩展、空库或可覆盖）→ 停写与排空（主机停止认领，控制面只读）→ 一致性导出 →
导入与校验（行数、迁移修订、身份命名空间）→ 用 WP11 的切换步骤改连接 → 逐实例健康检查 → 恢复写入。

**回滚边界**：恢复写入之前可以切回旧库；恢复写入之后不切回旧库，避免两个库分叉，只能按恢复手册处理。
旧库保留只读直到管理员确认。迁移期间前端只读，并显示迁移进度；控制面在切换时会重启。

## 8. 横切要求

- **安全**：不新增任意命令、任意路径或任意 Compose 内容的入口；放宽 AGENTS.md §7 边界的取值不出现在任何模式里。
- **审计**：每个意图、每个本机操作、每次凭据和密钥变更都有审计记录，不含值。
- **兼容**：所有新字段可选；旧前端、旧后端、旧 runner、旧代理都退回今天的行为，不显示虚假的已生效。
- **门禁**：注册进 `SETTINGS`/`HOST_SETTINGS` 的变量同时更新分类表（门禁会要求）；部署代理改写 `.env` 不改变分类。
- **文档**：改 ENVIRONMENT.md、`deploy/release/README.md` 等站点页面时同步中文翻译并跑 `make docs-verify`。
- **验证记录**：每个工作包在 `docs/agents/reviews/` 记录机器、提交、命令和结果；不在本文回写「已完成」。

## 9. 未决问题

1. 托管 compute 主机是否也装代理（建议不装，继续用 SSH 执行器）。
2. `ADMIN_TOKEN` 能否发出部署意图，以及是否收窄它的权限（WP9）。
3. 代理的发布锁来源：读下游发行版的锁，还是在机器上导入。
4. 控制面是否会跨多台机器；若是，WP4 的实例集合和 WP11 的重启顺序按机器分组。
5. 控制面那份 `AGENT_EGRESS_PROTECTED_CIDRS` 是否改为从主机回报推导（WP7）。
6. 代理以哪个系统用户运行，是否需要 Docker 组之外的权限（Kata 安装需要 root；可能拆成一个只做 Kata 的特权辅助程序）。
