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
