# 个人模型连接：实现审查与自动化验收

日期：2026-10-28

实现提交：`82d494e0fc78fd1bfe2206e678703d30fd7fd5c8`

基线：`3f60b6afbb86c67422b3c237c3e9ab68ed2effac`

Claude Code（`claude-opus-5-5`、high）与 Codex 完成实现；Codex 独立审查、补充回归测试、运行数据库与浏览器验收，并将同一提交接回隔离工作区。未推送、未创建 PR、未部署。

## 验收范围

用户在「设置 → 我的设置 → 个人模型连接」配置自己的 Base URL、API key、协议和模型。连接仅本人可用，费用由所配置服务商按该 key 计费。登录、Agent 准入与计算资源限制仍沿用现有系统；独立账号和等待名单不在本次范围内。

新任务、续聊、排队消息、目标自动轮次及原生 Fork 保留模型及其来源；同名模型可来自不同连接。连接失效时不会静默切到 Gateway。Chat Completions 对应 OpenCode、Kilo、pi，Responses 对应 Codex；Claude Code 运行时暂不支持。个人模型仅使用 Auto 推理强度。

## 检查结果

| 检查 | 环境与命令 | 结果 |
|---|---|---|
| 后端完整检查 | Linux、Python 3.12；`umask 022; env -u CODEX_VERSION PYTEST_ADDOPTS='-n 4 --dist loadfile' make check` | Ruff、539 个源文件的 mypy 检查通过；12,515 项测试通过，7 项需要 root/setpriv 的测试跳过 |
| PostgreSQL 完整测试 | Linux、Docker；`make test-db-local` | 2,006 项通过，0 跳过 |
| 前端完整检查 | Node.js 22.22.2；`make ci-frontend` | 格式、ESLint、类型、构建保护均通过；4,441 项 Vitest、16 项 Playwright 通过 |
| 独立浏览器验收 | 本地 Chromium；两份新增 Playwright spec | 4 项通过，包含 1440px/390px 设置流程、来源刷新保留、同名切源和失效阻止提交 |
| 文档 | `make docs-translate`、`make docs-verify` | 英文及简体中文构建、翻译目录一致性检查通过 |

数据库完整测试在提交前完成；随后核对了测试后快照与实现提交，后端实现、迁移和数据库测试文件字节一致。最终前端完整检查包含独立补充的键盘回归修复。

已检查模型选择器及手机设置卡片截图，未见横向溢出或同名来源混淆。浏览器测试使用模拟 API，不代表真实服务商推理验收。

## 独立审查发现及修复

- 流式连接测试必须验证真实工具调用 ID、参数、随机结果和完整终止事件；保留 Responses 所需的前轮输出上下文。畸形 SSE、无效 UTF-8、压缩响应和超限正文不能伪装成测试通过。
- 来源通过真实 SQL 保留到管理员详情和会话用量。混合会话中的 Gateway 小计标明不完整，个人服务商费用未知时不显示为已确认的零费用。
- 排队消息不能只提供连接却省略或清空模型；只编辑正文则保留既有来源。
- 凭据版本贯穿认领和常驻绑定，避免轮换期间启动未被该回合脱敏的另一把 key。扫描历史仅包含本沙箱组实际使用的密钥版本；删除和轮换后仍加密保留，文案明确这些副本仍是真实上游凭据。
- 个人连接不可用时，续聊的按钮和 Enter 提交路径都受限。独立回归先复现键盘绕过按钮禁用，再验证修复；后端原有来源校验仍保留。
- `test_source_cancellation[metadata]` 的目录顺序问题在未修改基线复现。夹具现改为在遇到的第一个条目处触发取消，继续断言取消后没有额外元数据读取及输出；没有放宽生产逻辑或取消断言。

## 尚未验收及上线约束

- 未使用真实服务商 key，未在 Kata 主机运行真实 OpenCode/Codex 端到端任务；上线前仍需 staging 联调。
- `AGENT_MODEL_CONNECTIONS_ENABLED` 默认关闭。启用前必须完成所有主机及全部控制面副本升级；关闭开关只停止新准入，不是终止运行任务的开关。
- 存在个人连接或个人来源历史时不能直接回退旧控制面；迁移降级会拒绝此状态，以免历史任务被解释成 Gateway 来源。
- 默认模型显式固定仍仅支持 Gateway；个人来源通过「沿用上次」记忆。个人来源暂不支持提示词改写，费用总览保留 Gateway 用量不可用状态。

产品及实现细节见[设计记录](../specs/2026-10-27-user-model-connections.zh-CN.md)，运维步骤见[个人模型连接](../../developer/operations.md#owner-model-connections)。
