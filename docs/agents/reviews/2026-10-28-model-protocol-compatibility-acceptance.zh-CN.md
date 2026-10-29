# 个人模型连接协议兼容：追加验收

日期：2026-10-28

同一 PR #1029；兼容实现为 `cb68e81df`，数据库滚动升级修复为 `0defc9ff9`。
Claude Code（`claude-opus-5-5`、high）实现并在其 Linux 服务器自测；Codex 审查、整合并在独立 Linux 开发机验证。本记录不表示已经部署。

## 最终行为

用户仍在个人设置填写自己的 Base URL、API key、上游 API 类型和模型。原生协议优先；只有上游缺少 Agent 的原生协议、但提供 Chat Completions 时才转换。

| Agent | 原生上游 | 仅提供 Chat Completions 的上游 |
|---|---|---|
| Codex | Responses 直连 | 私有 Responses → Chat Completions 转换 |
| Claude Code | Anthropic Messages 直连 | 私有 Messages → Chat Completions 转换 |
| OpenCode / Kilo / pi | Chat Completions 直连 | 直连 |

设置页、测试按钮和任务模型选择器标识转换路径。连接记录中的协议仍表示上游实际提供的协议，不能因为存在兼容层而把 Responses 或 Messages 写成上游原生支持。连接失效、模型不匹配或无法承载请求时拒绝执行，不自动改用其他来源。

兼容服务与成员 CLI 一起运行，固定上游、模型及凭据，使用原有沙箱出站规则；CLI 退出时服务也退出。控制面签发成员专属的本地令牌，CLI 使用令牌访问回环服务，上游 key 由服务持有。与同一连接上的 Fork 成员区分令牌，不依赖上游 key 派生。纯转换代码从 HybridInference 的固定提交迁入，保留许可证、来源和 106 项上游回归；没有复制网关的路由、账号、配额或计费服务。

## 审查中修复的问题

- 数据库迁移原先在成员热表加列，导致旧版本进程在同一事务重用预编译查询时收到 `InvalidCachedStatementError`。改为独立的连接常驻绑定表，并按常驻代数匹配。未改动的滚动升级检查在修复前复现失败，修复后通过；轮换旧连接不能终止较新的 Gateway 常驻进程。
- 本地令牌改由平台密钥按所有者、成员、连接版本、客户端协议和模型签发；Fork 两个成员分别获得自己的启动字段，后续 claim 保持一致。直连不允许携带令牌，包括格式错误但非空的令牌。
- 保留 Codex 的命名空间工具与子 Agent 能力，转换工具定义、选择、调用和历史时使用一致映射，避免只支持普通函数名。
- 上游 `[DONE]` 后不再等待连接关闭；截断、未知停止原因、错误 choice、损坏事件及结束后的解析错误不能被报告为正常完成。CLI 取消请求时关闭上游连接，覆盖等待响应头及持续流两种情形。
- HTTP 服务放在 host 包，shared 包只保留纯转换代码；没有给导入边界测试添加豁免。保留容器在启动转换模式成员前按摘要安装可信 zipapp。
- 浏览器截图关闭动画，避免把模型下拉框过渡中的半透明状态当成最终 UI。

## 验证证据

Claude 服务器上的提交 `cb68e81df`：

- `make check`：12,806 通过，7 项需要 root/setpriv 的测试跳过；格式、lint 和 552 个源文件的类型检查通过。
- `make test-db-local`：2,008 通过，0 跳过。
- Node 22 `make ci-frontend`：4,451 项单元测试、22 项 Playwright 通过；其余 lint、格式、类型和构建检查通过。
- `make docs-translate`、中文翻译及 `make docs-verify` 通过。

Codex 独立开发机：

- 包含附加令牌校验修复的完整 `make check`：格式、lint、552 个源文件类型检查通过；12,806 项测试通过，7 项需要 root/setpriv 的测试跳过。
- 转换内核、HTTP 服务、启动器、连接检查、领域规则和 API 的 327 项相关回归通过。
- 项目固定 CLI 的 3 项真实进程验证通过：Codex 0.145.0 使用生产驱动相同的 app-server RPC，Claude Code 2.1.220 使用常驻 stream-json；真实 zipapp 对接隔离的 Chat 模拟上游。`exec_command` / `Bash` 实际执行，工具结果进入下一次 Chat 请求，回合完成；CLI 环境没有上游 key，兄弟成员令牌和上游 key 都得到 401，CLI 退出后服务关闭。第三项验证 Codex 命名空间工具被还原并由 CLI 分派。
- 1440 px 和 390 px 两项 Playwright 通过；检查了设置页、转换测试结果、原生优先的模型选项及提交载荷，并人工核对远端生成的截图。浏览器 API 使用模拟数据。
- 原样执行 `check_rolling_upgrade.py --base b49eb72fed0c8ee80585f0e99181cbb7f25b9a4e`，旧版本会话和 runner 在升级及重试后均通过。此前还独立跑过完整 PostgreSQL 测试，2,008 通过、0 跳过。
- Gitleaks 8.30.1 检查已跟踪文件的干净副本和 HEAD 的完整历史，无发现；没有增加文件路径豁免。

## 验收环境与范围

独立开发机起初缺少浏览器的 `libgbm`、`libasound` 和 Docker Compose，分别提取发行版依赖、安装校验过摘要的官方 Compose 到本次验收的私有目录。该机的独立 Python 所带 OpenSSL 无法初始化系统 OpenSSL 配置，在项目之外也能复现；仅对测试进程设置 `OPENSSL_CONF=/dev/null` 使用标准配置，另行确认 `CERT_REQUIRED` 和主机名校验仍开启。未修改系统配置或测试断言。

`AGENT_MODEL_CONNECTIONS_ENABLED` 仍默认关闭；主机与全部控制面副本升级后才可启用。登录和 Agent 准入仍依赖 Gateway，个人连接的费用由用户的上游 key 承担。兼容模式不提供服务商托管的搜索、文件或有状态 Responses 功能；托管搜索在 CLI 中关闭，历史 reasoning/thinking 不转发。尚未使用真实服务商 key，也未完成真实 Kata 端到端验收。

设计见[兼容层设计](../specs/2026-10-28-model-protocol-compatibility.zh-CN.md)；原生 Messages 的历史验收见[此前记录](2026-10-28-anthropic-model-connections-acceptance.zh-CN.md)。
