# 部署代理、凭据矩阵与轮换/换库流程设计（WP6–WP12 实现前评审）

- 日期：2026-10-30
- 依据：[实施计划](2026-10-30-unified-config-management-plan.zh-CN.md)、[P1](2026-10-29-env-inventory-and-gates.zh-CN.md) §8；AGENTS.md §7
- 性质：实现设计。本文先于 WP9–WP12 的代码写成，并按真实消费者逐条复核（§9）。与计划不同之处在 §10 列出。

## 1. 组成

| 部分 | 位置 | 职责 |
|---|---|---|
| 部署代理 | `deploy/agent/`（随发布配方分发） | 每台自管机器一个；只执行封闭操作集；持有引导存储 |
| 引导存储 | `<DEPLOY_ROOT>/agent/` | 身份、凭据、密钥对、本机策略、配置版本、发布锁、操作日志 |
| 共享契约 | `cloud_agent_shared/deployment_contract.py`（也进配方） | 操作名、参数模式、配置键模式、状态机；控制面和代理都用它校验 |
| 控制面 | `cloud_agent/application/deployments.py`、`api/admin_deployments.py`、`api/deploy_agents.py`、迁移 0109 | 意图、授权、审计、报告归并、发布编排 |

托管 compute 主机不装代理：控制面的 SSH 执行器（`SshHostBootstrap`）继续负责，渲染 `.env` 时改用同一份配置键模式校验。

## 2. 引导存储

```text
<DEPLOY_ROOT>/agent/
  identity.json        代理 ID、名称、角色、机器名、控制面地址、公钥
  credential           代理凭据（0600）
  key.pem              X25519 私钥（0600），只用于解开密封值
  policy.json          本机策略；只在机器上编辑，前端只读
  releases/<sha256>/   已导入的 release.json、recipe.tar.gz 及其校验记录
  config/<rev>.env     每个已应用的完整 .env；保留最近 20 个
  state.json           last_seq、当前配置版本、进行中的操作
  operations/<id>.json 每个操作的参数摘要（不含密封值明文）、阶段、结果
```

`<DEPLOY_ROOT>/.env` 始终是当前配置版本的副本，原子替换（写临时文件、fsync、rename）。接管已有部署时，现有 `.env` 记为版本 0，逐字节保留；模式不认识的键作为 `unmanaged_keys` 上报，代理不改写它们。

整个 `agent/` 目录属于恢复集合（备份与恢复手册）。

## 3. 协议

拉取模型：代理用自己的凭据（`cad_<id>_<secret>`，控制面只存 SHA-256）主动连接控制面。

| 端点 | 调用方 | 作用 |
|---|---|---|
| `POST /v1/agent/deploy/register` | 代理（一次性加入令牌） | 换取代理凭据；登记公钥、角色、机器名 |
| `POST /v1/agent/deploy/report` | 代理 | 上报观测事实、策略摘要、已导入发布、操作状态 |
| `GET /v1/agent/deploy/intents` | 代理 | 取发给自己的待执行意图（标记为 delivered） |
| `POST /v1/agent/deploy/intents/{id}/result` | 代理 | 上报一个意图的阶段和结果 |

意图字段：`intent_id`、`agent_id`、`seq`（每代理单调）、`operation`、`params`（无密钥）、`sealed`（用代理公钥加密的值）、`idempotency_key`、`created_by`、`expires_at`。

代理执行前检查：意图属于自己；`seq > last_seq`（重放）；未过期；操作在封闭集合内；参数通过契约校验和本机策略。任何一项失败都上报 `rejected`，不执行。`last_seq` 在开始执行前写入 `state.json`，所以崩溃后不会重复执行同一意图；重启后按 `operations/` 里的阶段继续或标记失败。

控制面按状态机只接受向前的转换：`pending → delivered → running → succeeded | failed | rejected`；`pending → expired | cancelled`。同一 `(agent_id, idempotency_key)` 再次提交返回已有意图。本机执行的操作上报时 `origin=local`，控制面记为「本机执行」，不覆盖管理员的期望状态。

## 4. 封闭操作集

| 操作 | 角色 | 参数 | 执行 | 证据 |
|---|---|---|---|---|
| `apply_config` | 任意 | `changes`（模式内的键）、`sealed` 键 | 校验 → 写新版本 → 换 `.env` → 只重建受影响的服务 → 健康检查；失败恢复上一版本 | 新版本号、服务状态、健康检查 |
| `rollback_config` | 任意 | `to_rev` | 同上，内容取历史版本 | 同上 |
| `restart_services` | 任意 | `services`（模式里的固定名字） | `docker compose up -d --no-deps --force-recreate <svc>` | 服务状态 |
| `release_prepare` / `release_prepare_database` / `release_activate` / `release_abandon` | 按发布说明 | `manifest_sha256` | 只用发布锁里已校验的清单；固定 argv 调配方的 `deploy.sh --phase` | `.deployed-sha` 等于清单提交；就绪围栏 |
| `kata_install` | host | `version`、`sha256` | 必须是本机策略里的已审查对；先排空；调用特权辅助程序 | 辅助程序回读的已安装版本和摘要 |
| `rotation_step` | 视流程 | `workflow`、`step`、`sealed` | 见 §6 | 每步的确认 |
| `database_check` / `database_switch` / `database_migrate` | control-plane | 见 §7 | 见 §7 | 探针与校验 |

没有任意命令、任意路径、任意 Compose 内容或任意 URL。命令一律是固定 argv，不经 shell。

## 5. 凭据矩阵

| 主体 | 识别 | 管理 API | 部署意图 | 签发/撤销日常凭据 | 轮换/换库 | 代理 API |
|---|---|---|---|---|---|---|
| `ADMIN_TOKEN` | 每请求比对部署环境 | 全部（现状不变） | 允许（它今天就是完整管理员） | 允许 | 允许 | 不允许 |
| Gateway 管理员会话 | 会话 + 新鲜的 `agent.admin` 决定 | 全部 | 允许 | 允许 | 允许 | 不允许 |
| 日常管理凭据（WP9 新增） | `caa_<id>_<secret>`，库里存 SHA-256，每请求查库 | 全部，除下列 | 仅当创建时授予 `deployment` 范围 | 不允许（防止自我续命） | 不允许 | 不允许 |
| on-call 令牌 | 现有专用门 | 不变（不升级为管理员） | 不允许 | 不允许 | 不允许 | 不允许 |
| 代理凭据 | `cad_<id>_<secret>` | 不允许 | — | — | — | 只能读写自己的意图与报告 |
| runner 主机凭据 | 现有 | 不允许 | 不允许 | 不允许 | 不允许 | 不允许 |

带密封值或不可撤回后果的意图（`apply_config` 含密钥、发布 activate、`kata_install`、轮换、换库）要求请求里带 `confirm` 字段等于操作名，审计记录主体、代理、操作、参数摘要，不记录值。

## 6. 轮换状态机

共用 `agent_rotation_operations`（operation_id、kind、state、step、cursor、key ids、started_by、日志）。每一步幂等，可从记录的 step 重入。

**设置根密钥**（数据族：设置行的 secret 字段；runner policy 的归档访问密钥）：

1. `separate`：每台控制面机器的代理把 `AGENT_SESSION_SECRET` 的当前字节写成 `AGENT_SETTINGS_ENCRYPTION_KEY`（本机复制，明文不经过控制面），重启 API 和 MCP。确认：所有当前实例报告 `root_key_source=dedicated` 且 `root_key_id` 等于之前回退的 id。
2. `stage`：浏览器生成新根密钥，分别用每台控制面代理的公钥密封；代理写入新值为主键、旧值为 `_PREVIOUS`，重启。确认：所有实例报告新 id 和旧的 previous id。解密先试主键再试 previous。
3. `reencrypt`：控制面分批（游标）用新键重新加密两个数据族，验证新键能解开每一条。
4. `retire`：代理删除 `_PREVIOUS` 并重启；确认所有实例不再报告 previous。
混合版本：开始前要求所有实例声明 `settings-key-rotation-v1`，且没有未识别的数据库会话。任何一步失败都停在该步，可重试；step 3 之前回退等于删除新键（数据仍是旧键加密），step 3 之后不再回退到只有旧键的状态。

**控制签名密钥**：新增 `control_signing_secret_previous`（只验证）。`stage`：新密钥成为签发密钥，旧密钥进 previous，令牌两者都验证；等待最长令牌寿命后 `retire` 清空 previous。不再要求主机重新加入。

**sandbox manager 令牌**：主机侧新增 `AGENT_SANDBOX_MANAGER_TOKEN_NEXT`（sandbox manager 同时接受）。`stage`：每台主机写入 next 并重启 sandbox manager（自管主机经代理，托管主机经 SSH 执行器重新渲染）；确认：主机心跳报告 `manager_token_next_id`。`switch`：控制面设置换成新令牌。`retire`：主机把 next 提升为主令牌、删 next。

**两把凭据加密密钥**：新增 `<key>_previous` 设置（只解密）。`stage` → `reencrypt`（每个数据族一个游标）→ `retire`。数据族由 `credential_cipher` 的域清单枚举，测试要求每个用这两把密钥的域都在清单里。

## 7. 数据库

**身份**：同一个库 = 同一个 PostgreSQL 系统标识符（`pg_control_system()`）+ 同一个数据库 OID + **双向活探针**。控制面经现有连接写入随机值 N1 到 `agent_database_probes`；代理经新连接读到 N1，再写入 N2；控制面经旧连接读到 N2。克隆的库可能系统标识符、OID、迁移版本和身份命名空间都相同，但看不到克隆之后写入的探针。任何一步不符或超时都失败关闭。

**同库连接更新（WP11）**：`database_check`（上面的探针）→ `database_switch`：写新配置版本（`AGENT_DATABASE_URL` 是密封值）→ 逐个重启控制面服务 → 实例报告确认。若同时轮换口令：代理用**旧连接**执行 `ALTER ROLE … PASSWORD`（新口令是密封值，只在代理内存里），旧口令随即失效；之后的失败只能**前滚**（用新口令重试），操作记录写明「旧口令已失效，不能回退」，不会声称回退成功。

**换库（WP12）**：
1. `preflight`：目标可连、版本兼容、**为空**（`public` 下没有表）；非空则拒绝，不覆盖。
2. `fence`：源库 `ALTER DATABASE … SET default_transaction_read_only = on`，并终止应用角色在源库上的其他会话。这同时挡住所有控制面进程、后台任务，以及经控制面写库的主机（主机不直连数据库）。
3. `copy`：`pg_dump` 源库到代理目录，再 `pg_restore` 到目标；命令是固定 argv，客户端程序路径来自本机策略。
4. `verify`：每张表的行数、`alembic_version`、身份命名空间在两边一致。
5. `switch`：同 WP11 写新连接并重启；目标可写。
6. `complete`：控制面在目标库记录操作完成。

源库在第 5 步之前出错：`unfence`（`RESET default_transaction_read_only`）恢复原状。第 5 步之后写入恢复在目标库，源库保持只读，**不自动切回**。操作日志在代理本地，所以控制面在迁移中重启、甚至源库丢失，代理都能从日志继续或给出确切状态。

## 8. 失去控制面与冷启动

- 冷启动：`python -m deploy.agent init --root <DEPLOY_ROOT> --role control-plane` 在本机生成身份和密钥；首次部署与今天 `deploy.sh` 相同。控制面起来后用管理员签发的加入令牌 `register`。
- 失去控制面：代理继续运行最后的配置；`status`、`apply --rev`、`release activate --manifest-sha256` 等本机命令在机器权限下执行，记录为本机操作，恢复后上报。
- 控制面更新自己：操作日志在代理本地；activate 期间 API 重启，代理在新进程起来后上报结果。

## 9. 评审：按真实消费者逐条复核

| 检查 | 结论 |
|---|---|
| `verify_admin_access` 的所有调用点都只认 `ADMIN_TOKEN` 和 Gateway 会话；新增日常凭据只加一个分支，不改前两者 | 现有恢复入口不变；日常凭据不能签发凭据 |
| on-call 令牌走 `verify_oncall_access` 自己的门 | 不受影响 |
| 代理凭据和 runner 凭据格式不同、表不同 | 互不能代替 |
| 设置根密钥的读者：`domain_cipher.settings_root_key()`（设置行）和 `sandbox_archive_credentials`（归档密钥） | 两个数据族，都在 §6 |
| 主机不直连数据库，只经控制面 API | 源库只读即挡住主机写入 |
| 沙箱网络和隔离（AGENTS.md §7） | 配置模式不含 `ALLOW_UNISOLATED`、`EXTRA_ARGS`；受保护网段只能增加；`apply_config` 对边界键只接受本机策略列出的值 |
| 密封值 | 浏览器加密；控制面库、日志、审计只见密文；代理把明文只写进 `.env`（0600）和 `config/<rev>.env`（0600） |

## 10. 与计划不同之处

1. 运行时注册表不是字符串型主机设置，而是一个控制面设置经心跳下发、主机回报（WP2 已实现）：它全部署只有一个值，没有按主机覆盖的意义。
2. 控制面不在「所有主机回报之后才切换」，而是在有主机未回报时拒绝发起更新（效果相同，状态更简单）。
3. 模型连接开关的证据除实例报告外还要求没有未识别的数据库会话：新表里没有行不能证明没有旧进程。

## 11. 收尾设计（接手后补齐，实现前评审）

本节补齐实现记录里「未实现」的五项：两把凭据加密密钥的轮换、主机凭据续期与签名密钥的无中断退役、托管主机的 sandbox manager 令牌、隧道 SSH 密钥、换库期间的维护状态与代理侧恢复。每项先列真实消费者，再定流程；§11.6 逐条复核。

### 11.1 两把凭据加密密钥（`source_control_encryption_key`、`compute_encryption_key`）

**数据族清单**（`cloud_agent/credential_families.py` 是唯一来源；测试从迁移后的真实库枚举所有 `*ciphertext*` 列和含 `value_ciphertext` 的 JSONB 列，要求每一列都在清单里或明确属于别的密钥）：

| 密钥 | 族 | 表与列 | 域 |
|---|---|---|---|
| 用户凭据 | GitLab 连接 | `agent_gitlab_connections.access_token_ciphertext / refresh_token_ciphertext` | `hybridinference/source-control/v1` |
| 用户凭据 | GitHub 账号 | `agent_github_accounts.access_token_ciphertext / refresh_token_ciphertext` | 同上 |
| 用户凭据 | OAuth state 的 PKCE | `agent_oauth_states.code_verifier_ciphertext` | 同上 |
| 用户凭据 | MCP 连接 | `agent_mcp_connections.access_token_ciphertext / refresh_token_ciphertext` | `hybridinference/mcp-connection/v1` |
| 用户凭据 | MCP 客户端密钥 | `agent_mcp_connections.client_secret_ciphertext`、`agent_mcp_oauth_flows.client_secret_ciphertext / code_verifier_ciphertext` | `hybridinference/mcp-oauth/v1` |
| 用户凭据 | 待批准调用参数 | `agent_mcp_invocations.arguments_ciphertext` | `hybridinference/mcp-invocation/v1` |
| 用户凭据 | 环境变量 | `agent_environment_version_variables.value_ciphertext`；`agent_environment_bindings.overrides / runtime_variables / runtime_secret_history` 里每个元素的 `value_ciphertext` | `ENVIRONMENT_DOMAIN` |
| 用户凭据 | SSH 服务器私钥 | `agent_ssh_servers.private_key_ciphertext / pending_private_key_ciphertext` | SSH 服务器域 |
| 用户凭据 | 模型连接 | `agent_model_connections.api_key_ciphertext` | 模型连接域 |
| 用户凭据 | 模型连接扫描副本 | `agent_model_connection_scan_keys.key_ciphertext` | 扫描域 |
| compute | 连接凭据 | `agent_compute_connections.secret_ciphertext` | `cloud-agent/compute-credentials/v1` |
| compute | 部署 profile 输入 | `agent_compute_deployment_profiles.secret_ciphertext` | 同上 |

**以 MAC 为键的族**（只用户凭据密钥有）：

| 族 | 性质 | 轮换处理 |
|---|---|---|
| 环境安装指纹 `setup_fingerprint`（`agent_environment_bindings`、`agent_environment_snapshots`，后者 `(environment_id, setup_fingerprint)` 唯一） | 持久化的查找键：快照复用按它匹配，退役逻辑按「当前配置的指纹 ≠ 快照指纹」退役快照 | 显式重建索引（`reindex` 步） |
| 翻译令牌 `translator_token` | 每次认领时派生，不入库；是进程复用身份的一部分 | 索引密钥切换时一次性变化，常驻进程按现有复用规则重启一次 |
| 脱敏缓存的 epoch | 进程内缓存键 | 换密钥即失效重建，无需处理 |

**密钥环语义**（`CredentialCipher`）：加密只用当前密钥；解密先当前、再 `<key>_previous`；`mac()` 用**索引密钥**——轮换的前半程是旧密钥，`switch_index` 之后是新密钥；`macs()` 返回两把各自的 MAC，供「是否仍是当前配置」这类判断在窗口期两者都认。三项都来自已保存设置（`*_previous`、`credential_index_uses_previous`，只由轮换流程写入，设置页不提供编辑器），每个进程随设置快照采纳。

**流程**（`credential_key` 工作流，参数 `key=user|compute`）：

1. 开始前：所有当前实例声明 `credential-key-rotation-v1`，没有未识别的数据库会话；该密钥已配置；新密钥 ≥ 32 字节且不同于当前值。新密钥由浏览器随机生成，经 HTTPS 交给管理 API，只以根密钥加密的形式进入工作流数据和设置行。
2. `stage`：一次设置更新写入新密钥、`_previous`=旧密钥、（用户密钥）`credential_index_uses_previous=true`。等待所有当前实例采纳该修订且都声明能力。
3. `reencrypt`：按族、按主键游标分批；每行用密钥环解开、用新密钥重新加密，`UPDATE … WHERE id=$1 AND col IS NOT DISTINCT FROM $old`（比较并交换），并发写入赢则跳过该行（它已是新密钥）。游标随工作流版本保存，崩溃后从游标继续；整轮结束后再扫一遍「只用新密钥能否解开」，不能解开的行（两把都解不开，轮换前就已不可读）只计数报告，不阻止退役，因为退役旧密钥不会让它们更不可读。
4. `switch_index`（仅用户密钥）：设置写 `credential_index_uses_previous=false`；等待全部采纳。
5. `reindex`（仅用户密钥）：对每个环境，在一个事务里用两把密钥分别计算其各绑定和当前配置的指纹，得到旧→新映射，`UPDATE` 绑定和快照；目标指纹的快照已存在（窗口期新建的）时不改旧行，旧行在退役后按现有规则退役。退役判断在窗口期接受两把密钥的指纹，所以这段时间不会误退役快照。
6. `retire`：设置清空 `_previous`；等待全部采纳后成功。

**失败与重入**：每步幂等；`reencrypt` 之前回退 = 设置写回旧密钥（数据仍可用旧密钥解开）；`reencrypt` 开始后不回退到只有旧密钥的状态。旧版本进程在窗口期出现（不声明能力）时验证步停下并点名。设置页的「不能直接替换」保护保留：普通保存仍拒绝替换这两把密钥，只有轮换流程能写。

### 11.2 签名密钥与主机凭据续期（F26 重新加入）

签名密钥签三样东西：主机凭据（`aht`，长期，库里存其 SHA-256）、工作令牌（`ajt`，认领时签发，随 attempt 存活）、常驻进程的 MCP 凭据（随 residency 存活）。

**主机凭据续期**：`agent_runner_hosts` 新增 `credential_key_id`（签发当前凭据的签名密钥标识；旧行为空=未知）、`pending_credential_hash / pending_credential_key_id`、`previous_credential_hash / previous_credential_expires_at`。

- 心跳请求声明 `host-credential-renewal-v1` 时，若主机的 `credential_key_id` 不等于当前签名密钥标识，或管理员对该主机请求了续期，应答带 `credential_renewal`（新凭据，只此一次）并把其摘要写进 `pending`（覆盖旧的 pending；应答丢失就下次再发）。
- 主机先把新凭据原子写入凭据文件（0600）再换用；同机多个 runner 副本共享文件，每次心跳和认领前重读文件。
- 鉴权接受当前、pending、未过期的 previous。出示 pending 时在同一条语句里提升：previous=旧当前（10 分钟后过期，给共享文件的副本和在途请求），当前=pending。下游语句里「凭据仍是当前凭据」的复核使用行里的当前摘要，所以重新注册（清空 pending/previous）后在途请求照样被拒。
- 管理员的单主机「更换凭据」= 设置续期请求；新凭据被出示后旧凭据在宽限期后失效。这就是计划 F26 的「签发新凭据 → 主机切换 → 撤销旧的」，不需要新的加入令牌。不支持续期的旧 runner 只能重新注册，页面如实显示。

**签名密钥工作流**：`stage`（新密钥签发，旧密钥只验证）→ `adopt`（全部实例采纳）→ `renew_hosts`（等每台已注册且未撤销的主机 `credential_key_id` 等于新密钥标识；离线、旧 runner、未知标识都阻塞并点名，管理员可以等它上线或撤销它）→ `drain_tokens`（等没有 `started_at` 早于采纳时刻的运行中 attempt，也没有 `residency_established_at` 早于该时刻的 hot residency）→ `retire`（自动：清空 previous 签名密钥和所有主机的 previous 凭据摘要）→ 成功。这些等待没有 10 分钟超时，页面持续显示阻塞者。原来的「退役」按钮只在证据满足时可用，不再是绕过证据的手段。

### 11.3 sandbox manager 令牌：托管主机经现有 SSH 执行器

- 覆盖：有部署代理的主机经代理；托管 compute 主机经 `SshHostBootstrap.apply_environment`（新的固定远端脚本，只改允许的键 `AGENT_SANDBOX_MANAGER_TOKEN`、`AGENT_SANDBOX_MANAGER_TOKEN_NEXT`，值经 stdin 的 JSON 传入、不进 argv；先备份 `.env`，只重建 `sandbox-manager` 与 `runner`，健康检查失败恢复备份）；两者都没有的自管主机仍拒绝开始并点名。
- 证据：控制面用**下一把令牌**调用每台主机 sandbox manager 的已认证 `/health`；通过即证明该主机已同时接受新旧两把。代理的 `secret_ids` 作为辅助证据。
- `promote` 后托管主机所属的已保存部署 profile 模板里的令牌也换成新值（compute 密钥重新加密），以后重新部署或换节点不会带回旧令牌。

### 11.4 隧道 SSH 密钥（F27）

三条隧道（推理、MCP、控制面），每台主机一把私钥。

- **托管主机**（现有 `SshTunnelSetup` 管理远端账户）：控制面生成新 Ed25519 密钥并先以设置根密钥加密检查点存入工作流（重试复用同一把；有其他轮换在进行时根密钥轮换拒绝开始）→ 远端脚本 `authorize_next` 把新条目加在旧条目旁（同样的 `restrict,port-forwarding,permitopen` 限制，标记带 setup id 和角色）→ `apply_environment` 把新私钥写到主机 `.compute-secrets/<role>.key` 并只重启对应隧道服务，失败恢复旧文件 → 证据：主机继续心跳（控制面隧道）、隧道服务运行 → `retire_previous` 只留新条目 → 已保存 profile 的文件换成新私钥。
- **自管主机**（部署代理）：主机代理在本机生成新密钥（私钥不出机器），上报公钥；远端是控制面机器且其代理的本机策略声明了该隧道账户的 `authorized_keys` 时，由控制面代理加新条目，否则工作流停在「请在远端加入这把公钥」并显示完整的受限条目，管理员确认后继续；主机代理先用新密钥做一次认证测试（固定 argv，`BatchMode`、`IdentitiesOnly`、指定 known_hosts）再换文件并重启隧道，失败恢复；最后撤销旧条目（自动或提示人工）。

### 11.5 换库：维护状态与代理侧恢复

- **维护状态**：新表 `agent_maintenance`（单行）。工作流在下发迁移意图前写入 `state=migrating`，并等每个当前进程在实例报告里确认看到（`maintenance_seen`），超时则中止、不碰源库；每个控制面进程每 2 秒读一次，维护中时中间件对写请求（非 GET/HEAD/OPTIONS，部署代理端点除外）回 503 + `Retry-After` 和结构化原因，写到只读库的漏网请求也回同样的 503；前端显示全局维护横幅和进度。读请求继续由只读的源库回答。主机的认领和心跳写入同样被拒，主机按现有退避等待。
- **进度**：源库只读期间代理的报告无法入库；`/deploy/report` 遇到只读事务时把这台代理的迁移阶段放进进程内的易失进度并回 200 `{"persisted": false}`，状态端点合并显示，并标明「来自本进程的最近报告」。
- **解除只读**：被隔离的库里新会话默认只读，`ALTER DATABASE` 会被拒绝，所以 `unfence` 先把本会话设为读写。失败时删除本次迁移留下的不完整副本（预检时目标是空的）。
- **代理侧恢复**：迁移意图的密封目标连接以**密文**写进本机操作日志（0600，只有本机 `key.pem` 能打开），不写明文；代理重启时自动续做 `running` 的迁移，`database resume` 不再要求重新输入目标连接。其他被重启打断的操作标记为失败并说明「代理重启时中断」。意图的最终结果在送达控制面前一直保留「待送达」，每轮同步重发，所以控制面在切换中重启也不会丢掉结果。
- **维护标记的收尾**：代理在 `verify` 之后、切换之前，用 psql 在目标库把维护行写成 `completed`（目标库里的那行是导出时的副本）；切换失败回源库时在解除只读后同样清除源库的维护行。恢复写入之后不切回源库（不变）。
- **停机说明**：切换时控制面服务重启，页面会断开几十秒到几分钟，重连后从目标库的工作流和意图状态恢复显示；这是服务重启的真实停机，与工作流是否受支持是两回事，页面和文档分开说明。

### 11.6 评审

| 检查 | 结论 |
|---|---|
| 每个用这两把密钥的域都在清单里 | 代码扫描测试（`CredentialCipher(` 的每个域常量）+ 真实库列枚举测试 |
| 唯一约束 `(environment_id, setup_fingerprint)` | 重建索引遇到冲突不改旧行，不放宽约束 |
| 存储的凭据不被清除 | 重加密只写比较并交换的新密文；不可读行只报告 |
| 旧进程 | 能力声明 + 未识别会话检查；验证步对不声明能力的实例停下 |
| 主机凭据 | 鉴权仍是签名 + 库内摘要两道；新增的 pending/previous 只扩大到同一主机自己的续期凭据，宽限期有上限；重新注册清空两者 |
| 工作令牌与 residency 凭据 | 只按库内时间证据等待，不提前退役 |
| SSH 执行器 | 固定脚本、允许键白名单、值走 stdin；与代理共用 `check_rendered_environment` |
| 隧道密钥 | 私钥只在生成方（控制面检查点为根密钥加密，记入已保存 profile 后删除）；远端条目限制与首次设置相同 |
| 重启 | 主机上的重启先排空 runner 再排空 sandbox manager（无强制杀死期限），逐台进行；Kata 安装同样排空 |
| 新增不可变例外 | 环境版本值只在声明了 `cloud_agent.credential_reencryption` 的事务里、且只有 `value_ciphertext` 变化时可以改写；其他写入仍被拒绝 |
| 维护状态 | 只拒绝写；部署代理端点和 `ADMIN_TOKEN`/会话的读访问不受影响；不保存明文目标连接 |
