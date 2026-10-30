# 统一前端配置管理：实现记录（WP1–WP12）

- 日期：2026-10-30
- 依据：[实施计划](../specs/2026-10-30-unified-config-management-plan.zh-CN.md)、[部署代理与流程设计](../specs/2026-10-30-deployment-agent-and-workflows-design.zh-CN.md)、[P1](../specs/2026-10-29-env-inventory-and-gates.zh-CN.md)
- 基线：`1be1cfc6`；本记录描述其后的实现提交
- 性质：实现记录。完整门禁的命令、结果和日志记在交付目录（`delivery.md`、`evidence/`），不回写到这里。

## 覆盖清单

| WP | 实现入口 | 测试 |
|---|---|---|
| WP1 普通设置与导入欠账 | `setting_registrations.py`（流上限、API 文档、模型连接、注册表、上一把签名密钥）；`settings.MIGRATED_SETTINGS` 收进 8 个迟注册的名字；升级导入遇到非法值导入默认值并记名；`app._install_api_docs` 按请求判断 | `test_settings_upgrade_imports.py` |
| WP2 主机设置扩展 | `HostSetting lease_ttl_s` 与 `LATE_IMPORT_KEYS`（0107 `settings_late_imported`）；心跳应答带 `runtime_registry_url`、主机回报 `config_reports`；`registry_lagging_hosts` 拒绝更新；`local_settings.local_shared_resident_capacity` 修 P1 欠账 | `test_host_late_settings.py`、`test_host_registry_report.py`、`test_host_settings_import.py`（DB）、`test_admin_runtime_upgrades_api.py` |
| WP3 管理目录与生效元数据 | `management_catalog.py`、`GET /admin/agent/management/catalog`；`Setting.apply_mode/apply_description`，读者核对过的设置标注 | `test_management_catalog.py`（含「每个非平台/工具名字都属于某个目录项」） |
| WP4 实例上报 | `instances.py`、0108、`GET /admin/agent/management/status`；会话 `application_name`；`fleet_supports` 失败关闭（陈旧、缺能力、未识别会话、无报告） | `test_control_plane_instances.py`、`test_control_plane_instance_store.py`（DB） |
| WP5 统一前端 | Admin → Deployment（`admin/deployment/`）；设置字段显示生效方式；浏览器内密封（`adminManagement.ts`） | `adminManagement.test.ts`（打开 Python 密封的值）、`DeploymentManagement.test.tsx` |
| WP6 部署代理与引导存储 | `deploy/agent/`（store、client、cli、operations、system、README、systemd 示例）；`deployment_contract.py`（配方内为逐字节副本）；0109；`api/deploy_agents.py`、`api/admin_deployments.py` | `test_deploy_agent.py`、`test_deploy_agent_contract.py`、`test_deployment_agents.py`（DB，端到端） |
| WP7 受控应用 | `Executor.apply_config/rollback_config`（健康检查失败恢复上一版本）；边界键按本机策略、受保护网段只增；托管主机渲染经 `check_rendered_environment` | 同上 + `test_a_managed_server_environment_follows_the_same_key_rules` |
| WP8 发布、镜像与 Kata | `releases.py`（固定摘要导入、使用前复核、旧数据库修订的未验证回退拒绝）；`workflows.start_rollout`（主机先于控制面）；`kata-install-helper.sh` | `test_deploy_agent.py`（activate 以 `.deployed-sha` 为证据等）、`test_deployment_agents.py`（滚动顺序、主机失败不切控制面） |
| WP9 管理员凭据 | 0110、`api/admin_credentials.py`、`verify_admin_access` 新分支；`ADMIN_TOKEN` 轮换流程 | `test_admin_credentials.py`（DB）、`test_rotation_and_database_workflows.py` |
| WP10 密钥轮换 | `key_rotation.py`：根密钥（分离→暂存→两族重加密→退役）、sandbox manager 令牌双令牌窗口、签名密钥只验证的上一把；`deploy/agent/rotation.py` | `test_rotation_and_database_workflows.py`（根密钥全流程，DB）、`test_deploy_agent.py` |
| WP11 同库连接更新 | 0111 探针；`database_workflows.py`；`deploy/agent/database.py`（PG* 环境变量传参、SCRAM 校验值经 stdin、口令轮换后只前滚） | 真实数据库上：克隆（另一个库）失败、同一库换写法通过 |
| WP12 换库迁移 | `database_migrate`：非空目标拒绝、源库只读并结束会话、dump/restore、逐表行数与迁移修订比对、切换；步骤记日志可本机续做 | 真实数据库上的完整迁移（`postgres:15` 镜像里的 `pg_dump/pg_restore`） |

## 与计划不同之处（已在设计文档 §10 记录）

1. 运行时注册表是一个控制面设置经心跳下发，而不是字符串型主机设置。
2. 模型连接开关的证据还要求没有未识别的数据库会话。
3. 控制面在有主机未回报注册表时拒绝更新，而不是延后切换。

## 未实现与限制

- **两把凭据加密密钥的轮换没有实现。** 它们保护的数据里有以 MAC 为索引的族（模型连接扫描键、用于快照复用的环境值 MAC），重新加密必须同时重算索引并挡住并发写入，需要单独设计。设置页和目录如实说明，这两把密钥设置后仍然不能替换。
- **没有部署代理的服务器**（包括托管 compute 服务器）存在时，sandbox manager 令牌轮换拒绝开始，并列出这些服务器；不会在它们身上制造中断。
- 签名密钥轮换的 `retire` 之后，旧密钥签发的主机凭据和进行中的工作令牌失效，需要重新注册主机；这一步要求管理员确认，不自动发生。
- 迁移中途代理崩溃后，续做需要在机器上用 `database resume` 重新输入目标连接（密封值不在本机持久保存）。
- 换库期间源库只读，控制面的写请求会失败，直到切换完成；没有单独的维护页。
- Kata 辅助程序、sudoers 条目和 systemd 单元只随配方提供示例，没有在任何机器上安装。
- 所有部署与数据库测试使用临时目录、记录的 Docker 和一次性测试数据库；真实主机、真实 Kata、真实发布注册表和多台控制面机器的验收没有做，需要在环境切换时完成。
