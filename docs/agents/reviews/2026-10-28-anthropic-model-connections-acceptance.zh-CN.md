# Anthropic Messages 个人模型连接：追加验收

日期：2026-10-28

实现提交：`57d93ad02fc2005d6d9068f306e69a912afc75c5`；原有个人连接实现为 `82d494e0fc78fd1bfe2206e678703d30fd7fd5c8`。

Claude Code（`claude-opus-5-5`、high）补齐原生 Anthropic Messages 与 Claude Code 支持；Codex 独立审查、补充异常流/浏览器/PostgreSQL 回归、验证固定 CLI 的请求路径与头部，并整合到同一功能分支。本记录不表示已部署。

## 行为

用户在「设置 → 我的设置 → 个人模型连接」配置自己的 Base URL、API key、模型和 API 类型。新增 `anthropic_messages` 对应 Claude Code；原有 Chat Completions 对应 OpenCode、Kilo、pi，Responses 对应 Codex。使用原生接口，不转换协议；HybridInference 可以作为用户配置的兼容上游。推理费用走用户提供的 key，登录与 Agent 准入仍沿用 Gateway。

Anthropic 地址规范化只去掉结尾的一个 `/v1`，保留其他前缀；连接测试和 CLI 访问同一个 `/v1/messages?beta=true`。保存、测试、任务提交、续聊、认领与常驻进程继续携带连接来源；连接与运行时不兼容时阻止提交，不回退。

## 验证

- Linux 完整后端检查：`umask 022; env -u CODEX_VERSION PYTEST_ADDOPTS='-n 4 --dist loadfile' make check`，Ruff、类型检查通过；12,572 项测试通过，7 项需要 root/setpriv 的测试跳过。
- PostgreSQL 完整测试：`make test-db-local`，2,008 项通过，0 跳过；包含 Messages 单协议及三协议连接的保存、所有者隔离与 Claude Code 续聊来源。测试后核对了存储、迁移和集成测试的 335 个文件，内容未变化。
- Node.js 22.22.2 前端完整检查：`make ci-frontend`，ESLint、格式、类型、构建保护通过；4,445 项 Vitest 和 20 项 Playwright 通过。
- `make docs-translate` 与 `make docs-verify` 完成，英文及简体中文文档与翻译目录通过检查。
- Codex 独立使用项目固定的 Claude Code 2.1.220，对回环模拟服务发起请求。域名、`/v1`、`/anthropic`、`/anthropic/v1` 四种输入均生成预期路径，使用 `x-api-key` 和 `anthropic-version: 2023-06-01`，没有 Bearer 凭据。模拟服务故意返回 401；此项仅验证实际 CLI 的请求路径和头部。
- 已检查桌面表单、模型来源选择器和手机中文连接卡片的浏览器截图；中英文完整流程验证保存、连接测试、Claude Code 提交以及切换不兼容 Agent 后按钮和 Enter 均不能提交。浏览器使用模拟 API。
- PR 的 Gitleaks 把两个固定测试假 key 识别为通用 API key；仅对 `generic-api-key` 规则精确豁免这两个完整值，没有豁免路径。使用与 CI 相同的 8.30.1 验证当前文件和完整 Git 历史均无发现，改动假 key 数字后的反例仍被检出；凭据与扫描工作流的 12 项测试通过。

## 独立审查修复

- 拒绝在 thinking 内容块中发送 text 增量，以及在消息结束增量之后继续发送内容；两项用例先复现误判，再确认修复。
- 第一轮必须只有一个被提供的工具调用；第二轮必须没有待处理的工具调用。额外工具或仍请求工具的回答不能因包含随机验证值而被标为成功；两项用例先复现再修复。
- 原生 Messages 流验证开始、内容块、停止原因及结束事件，拒绝截断、错误事件、错误工具 ID/参数；保留工具结果关联和带签名的 thinking。
- 模型和 API 下拉框通过独立 `id`/`htmlFor` 关联标签，避免可访问名称包含选项内容；保留严格的中英文标签定位与协议值断言。

## 上线边界

`AGENT_MODEL_CONNECTIONS_ENABLED` 仍默认关闭。启用前须升级全部主机及控制面副本。尚未提供真实服务商 key，也未在 Kata 中完成真实 Claude Code 推理任务；本轮不代表真实服务商或生产端到端验收。既有密钥隔离、版本失效、脱敏和实际使用密钥扫描历史边界继续适用。

原有功能验收见[此前记录](2026-10-28-user-model-connections-acceptance.zh-CN.md)；产品与实现约定见[设计记录](../specs/2026-10-27-user-model-connections.zh-CN.md)。
