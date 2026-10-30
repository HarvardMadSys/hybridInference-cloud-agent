# 统一前端配置管理：实现记录（WP1–WP12）

- 日期：2026-10-30
- 依据：[实施计划](../specs/2026-10-30-unified-config-management-plan.zh-CN.md)、[部署代理与流程设计](../specs/2026-10-30-deployment-agent-and-workflows-design.zh-CN.md)（§11 是收尾设计）、[P1](../specs/2026-10-29-env-inventory-and-gates.zh-CN.md)
- 基线：`1be1cfc6`；本记录描述其后的实现提交
- 性质：实现记录。完整门禁的命令、结果和日志记在交付目录（`delivery.md`、`evidence/`），不回写到这里。

## 覆盖清单

| WP | 实现入口 | 前端入口 | 测试 |
|---|---|---|---|
| WP1 普通设置与导入欠账 | `setting_registrations.py`；`settings.MIGRATED_SETTINGS` 收进 8 个迟注册的名字；`app._install_api_docs` 按请求判断 | 设置页 | `test_settings_upgrade_imports.py` |
| WP2 主机设置扩展 | `HostSetting lease_ttl_s`、`LATE_IMPORT_KEYS`（0107）；心跳下发 `runtime_registry_url`、回报 `config_reports`；`registry_lagging_hosts` | Servers、设置页 | `test_host_late_settings.py`、`test_host_registry_report.py`、`test_host_settings_import.py`（DB） |
| WP3 管理目录 | `management_catalog.py`、`GET /admin/agent/management/catalog`；`Setting.apply_mode` | 「Where configuration lives」、设置字段注记 | `test_management_catalog.py` |
| WP4 实例上报 | `instances.py`、0108、`GET /admin/agent/management/status`；`fleet_supports` 失败关闭 | 「Saved and adopted」、设置页「In use by n of m」 | `test_control_plane_instances.py`、`test_control_plane_instance_store.py`（DB） |
| WP5 统一前端 | Admin → Deployment；`settingsManagement.tsx`；`MaintenanceBanner.tsx` | 见下节 | `DeploymentManagement.test.tsx`、`settingsManagement.test.ts`、`MaintenanceBanner.test.tsx`、`e2e/deployment-management.spec.ts`（真实浏览器） |
| WP6 部署代理与引导存储 | `deploy/agent/`；`deployment_contract.py`；0109；`api/deploy_agents.py`、`api/admin_deployments.py` | Machines、Enroll | `test_deploy_agent.py`、`test_deploy_agent_contract.py`、`test_deployment_agents.py`（DB） |
| WP7 受控应用 | `Executor.apply_config/rollback_config`；托管主机渲染经 `check_rendered_environment` | `MachineConfiguration.tsx`：按语义项、字段有类型；有专门流程的键不在这里 | 同上 |
| WP8 发布、镜像与 Kata | `releases.py`；`workflows.start_rollout`；`kata_install` 无强杀排空 runner；`kata-install-helper.sh` | Desired release、Machines | `test_deploy_agent.py`、`test_deployment_agents.py` |
| WP9 管理员凭据 | 0110、`api/admin_credentials.py`、`verify_admin_access`；`ADMIN_TOKEN` 轮换 | Administrator credentials、Keys and credentials | `test_admin_credentials.py`（DB）、`test_rotation_and_database_workflows.py` |
| WP10 根密钥 | `key_rotation.py`（分离→暂存→重加密→退役）；`deploy/agent/rotation.py` | Keys and credentials | `test_rotation_and_database_workflows.py`（DB） |
| WP10 两把凭据加密密钥 | `credential_key_rotation.py`、`credential_families.py`、`repositories/credential_rotation.py`、`environment_fingerprints.py`、`CredentialCipher` 密钥环、0112 | 同上，步骤和计数在 Operations | `test_credential_key_rotation.py`（DB：真实 schema 上每一族往返、指纹重建、不可变触发器）、`test_credential_key_ring.py`（含代码里每个 cipher 域的扫描） |
| WP10 签名密钥与主机凭据续期 | `signing_key_rotation.py`、`_sql/host_credentials.py`、`repositories/host_credentials.py`、`api/hosts.py`、`cloud_agent_host/enrollment.py` | 同上；Server credentials 表、单台 Renew | `test_host_credential_renewal.py`（DB，真实路由）、`test_host_credential_renewal_runner.py` |
| WP10 sandbox manager 令牌 | `sandbox_token_rotation.py`、`managed_hosts.py`、`SshHostBootstrap.apply_environment`、`host_apply_scripts.py` | Keys and credentials | `test_managed_host_rotations.py`（DB）、`test_host_apply_script.py`（真实运行远端脚本） |
| WP10 隧道 SSH 密钥 | `tunnel_key_rotation.py`、`setup_tunnels.rotate_*`、`deploy/agent/rotation.py` | 同上；远端确认面板 | `test_managed_host_rotations.py`（托管、人工远端、自管）、`test_compute_setup_tunnels.py`、`test_deploy_agent.py` |
| WP11 同库连接更新 | 0111 探针；`database_workflows.py`；`deploy/agent/database.py`（口令可重试、内置库 `AGENT_POSTGRES_PASSWORD` 同步） | Database：从机器上报的连接开始的表单、一键轮换口令 | `test_rotation_and_database_workflows.py`（真实库）、`test_deploy_agent.py` |
| WP12 换库迁移 | `maintenance.py`、0112 `agent_maintenance`、`database_migrate`（封存连接的日志、自动续做、部分副本清理、`unfence`） | Database、维护面板、全局横幅 | `test_database_maintenance.py`（真实两个库：端到端、失败重开、崩溃续做、本机 unfence）、`test_maintenance.py` |

## 与计划不同之处

见设计文档 §10。§11 记录收尾设计；隧道检查点和维护确认的实现细节也写在那里。

## 收尾时修掉的问题

这些都是核对真实消费者时发现的，不是计划里列出的：

- `ALTER DATABASE … RESET` 在被隔离的库里本来就跑不通（新会话默认只读），原来的 `unfence` 和失败路径都恢复不了源库。
- 环境版本值有不可变触发器，重加密会被拒绝；现在只放行声明过的、只改密文的写入。
- 环境指纹缓存没有区分密钥，换索引密钥后会一直返回旧指纹。
- GitLab、GitHub 客户端和 compute 存储把 cipher 缓存到进程结束，读不到轮换里的新密钥环。
- 主机重启用 Compose 的 stop 宽限期，长回合会被强杀；现在和 `sandbox_host.sh` 一样先无期限排空。
- 两次推进同时创建同一个意图会撞唯一约束。
- 代理重启后 `running` 的操作和发送失败的结果都没人处理。
- 设置的管理目录写好了但前端没有读；机器配置是变量选择器，且能绕过专门流程改 `ADMIN_TOKEN`。

## 已知限制（如实保留，不是待办）

- **人工远端**：部署代理和 SSH 执行器都够不到的隧道远端（常见的是推理网关），只能由管理员按页面显示的条目手工增删再确认。没有控制面和主机之外的执行器可用。
- **既没有代理、部署输入也不在数据库里的服务器**：sandbox manager 令牌和隧道密钥轮换拒绝开始并点名。这类服务器的令牌在部署配置文件里，控制面改不到，也不应该改。
- **换库进度**：源库只读期间代理的进度只存在收到报告的那个进程里，多台 API 进程时可能看不到，页面写明了；代理的日志才是权威记录。
- **多台控制面换库**：领头机器切到目标库后，其他机器的代理要连到新进程才能拿到切换意图，负载均衡下可能要多等几轮。
- **校验范围**：换库比对行数、迁移修订和身份命名空间，没有逐行内容校验。
- **环境指纹窗口**：`switch_index` 到 `reindex` 之间快照复用可能未命中而重新构建，不影响数据，也不会误退役快照。
- **翻译令牌**：索引密钥切换时变化一次，常驻进程按现有复用规则重启一次。

## 验证范围

所有部署与数据库测试使用临时目录、记录的 Docker、一次性测试数据库和测试用的远端替身。**没有**在真实主机、真实 Kata、真实发布注册表、真实 SSH 远端或多台控制面机器上验收：

- 远端应用脚本在临时目录和替身 `docker` 上真实运行过，隧道远端脚本的账户逻辑单独测试过，但都没有连过真实 sshd。
- 换库的 PostgreSQL 客户端程序是真实的（`postgres:15` 镜像），Compose 是记录的。
- 浏览器验证使用生产构建的 Next 应用和管理 API 夹具；管理 API 本身另有真实库上的测试。

这些属于环境切换验收。
