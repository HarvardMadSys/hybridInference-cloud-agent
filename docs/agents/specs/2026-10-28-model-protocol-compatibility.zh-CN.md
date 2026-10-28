# 个人模型连接的协议兼容层（Chat Completions 上游）设计

日期：2026-10-28
状态：已实现（本分支），部署开关仍是 `AGENT_MODEL_CONNECTIONS_ENABLED`，默认关闭；用固定版本的真实
CLI 和本地假上游验证过，未做真实服务商和 Kata 主机验收

## 一、要解决的问题

个人模型连接原来只按「运行时说什么协议，连接就要声明什么协议」匹配：只提供 Chat Completions 的
连接只能给 OpenCode、Kilo、pi 用。很多服务商（自建 vLLM、各类 OpenAI 兼容服务）只有
Chat Completions，用户也希望用 Codex 和 Claude Code。

本次在**成员自己的沙箱进程里**加一个私有的协议转换层，只做两条明确的路径：

| 运行时 | 客户端协议 | 连接只声明 `chat_completions` 时 | 连接声明了原生协议时 |
|---|---|---|---|
| OpenCode / Kilo / pi | Chat Completions | 原生直连 | 原生直连 |
| Codex | Responses | **转换**：Responses → Chat Completions | `responses` 原生直连 |
| Claude Code | Anthropic Messages | **转换**：Messages → Chat Completions | `anthropic_messages` 原生直连 |

不是任意协议之间的 N×N 转换，也不声称上游具备 Responses 或 Messages 的全部能力。不提供公开推理
服务、通用转发代理、模型或服务商回退、路由池、计费或额度系统。推理本身不需要 HybridInference
账号或连接；登录和 Agent 准入仍和原来一样经过 Gateway，本次没有改变。

## 二、计划（plan）：一个函数决定怎么连

`cloud_agent_shared/model_connections.protocol_plan(runtime, protocols)` 返回
`ProtocolPlan(client_protocol, upstream_protocol)`，`translated` 表示是否经过转换：

1. 运行时的客户端协议在连接声明的协议里 → 直连（`client == upstream`）；
2. 否则，客户端是 Responses 或 Anthropic Messages、且连接声明了 `chat_completions` → 转换；
3. 否则不可用（`protocol_unsupported`）。

**原生优先**：同时声明 `responses` 和 `chat_completions` 的连接，Codex 一定直连 Responses。

连接保存的 `protocols` 始终描述**上游**真实提供的协议，转换不会改写它，也不会让原生测试冒充。
准入、后续消息、排队消息、goal 轮次、领取、常驻凭据、原生 Fork、`/config` 和设置页、连接测试都
经由 `domain.require_usable` → `protocol_plan` 得到同一个计划。计划随认领载荷交给主机
（`ModelConnectionAccess.client_protocol`，直连时不出现，直连的认领与以前完全相同），进入进程
固定的启动字段 `inference_client_protocol` 和该成员的本地令牌 `inference_local_token`（见第五节），
参与「能否复用进程」的判断：连接协议的改动改变计划，
常驻进程随之重启；连接编辑提升 `credential_revision` 时旧进程照旧被关闭。同名模型在不同连接上
仍是不同来源；个人连接永远不使用 Gateway grant，也不回退。

主机能力：新主机声明 `model-protocol-translation-v1`。转换计划的回合只交给声明了它的主机；只声明
`model-connections-v1` 的主机拿到这样的认领会被退回（不消耗尝试次数）。

## 三、转换代码：从 HybridInference 抽取的纯协议内核

纯转换代码放在 `apps/backend/cloud_agent_shared/protocol_compat/`，只依赖 Python 标准库（沙箱里是
Debian bookworm 的 Python 3.11），不做任何 I/O，控制面和沙箱用同一份代码。沙箱里的启动器和本地服务
（套接字、线程、HTTPS）是主机包的 `apps/backend/cloud_agent_host/protocol_translator.py`，只导入标准库
和这个纯包；共享包的 I/O 边界测试不为它开任何豁免：

- 来源：`HarvardMadSys/hybridInference` 提交 `df40a9a82a6d5a39a9ffbdfebde3650f94cb5146`，MIT 许可，
  许可证原文随包保存在 `LICENSE.hybridinference`，`NOTICE.md` 列出逐文件对应关系和改动。
- 抽取：`serving/responses_translator.py`（Responses ↔ Chat）、
  `serving/adapters/anthropic_translator.py`（Messages ↔ Chat）、`serving/utils/messages.py`、
  `serving/utils/token_utils.py`。除导入路径外，为了满足本仓库的复杂度上限（不能新增抑制注释），
  把几个函数的循环体拆成辅助函数，行为不变；去掉了 Gateway 计费用的
  `extract_anthropic_usage_from_sse`。上游的 106 个翻译器回归测试只改导入后迁入
  `tests/unit/protocol_compat/test_*_upstream.py`，全部通过。`claude_format.py`（反方向）和
  `tokens.py`（依赖 tiktoken）没有用到，不抽取；Gateway 的路由、鉴权、数据库、路由池和计费都不引入。
- 维护：这是一次性复制，不是两个产品共享的依赖，HybridInference 仓库没有改动或发布。上游修复按
  `NOTICE.md` 记录的提交号人工比对后合入，保留拆分出的辅助函数，并跑迁来的回归测试。
- 本仓库新写的部分：`gate.py`（请求校验：不支持的内容**明确拒绝**，不悄悄丢弃）、
  `namespaces.py`（命名空间工具的扁平化与还原）、`stream.py`（上游流完整性守卫）；主机包的
  `protocol_translator.py`（沙箱内的启动器和本地服务）和 `compat_helper.py`（打包与安装）。

### 支持的子集（无状态、整段历史重放）

Responses → Chat（Codex）：
- 支持：`instructions`、`input` 中的 `message`（`input_text`/`output_text`/`input_image`）、
  `function_call`、`function_call_output`；`type: "function"` 工具，以及 `namespace` 工具里的
  function 工具（Codex 0.145.0 的子 Agent 工具 `multi_agent_v1` 和平台转发的 MCP 工具
  `mcp__session`）；`tool_choice`（含指向命名空间工具的强制调用）、`text.format`、
  `max_output_tokens`、`temperature`、`top_p`、`parallel_tool_calls`、流式与非流式。
- 命名空间工具在上游用扁平名 `<命名空间>__<名称>`；含非法字符、超长或与其他工具重名时追加摘要
  后缀，加了后缀仍冲突就拒绝。返回的流事件、最终响应和非流式响应里的调用都还原成原来的 `name` 加
  `namespace`；历史里的调用按同一规则映射，所以前后请求一致。
- 拒绝（400，说明原因）：`previous_response_id`、`conversation`、`background`、`prompt` 模板；
  非 function 工具（`web_search`、`custom`、`local_shell`、`file_search` 等，包括命名空间里的）；
  `input_file`、`refusal`、音频等内容；未知输入项（如 `item_reference`、`custom_tool_call`）。
- 不转发但如实说明：`reasoning` 输入项（加密推理内容，Chat 没有对应字段）和
  `reasoning`/`include`/`store`/`client_metadata`/`prompt_cache_key` 等元数据；输出里不产生推理项。
- Codex 在转换模式下启动时加 `web_search="disabled"`：0.145.0 默认请求带托管的 `web_search` 工具，
  Chat 上游无法提供。子 Agent 等其他功能不关闭。

Messages → Chat（Claude Code）：
- 支持：`system`（文本块）、`text`/`image`/`tool_use`/`tool_result` 内容块；无类型（自定义）工具；
  `tool_choice`（含 `none`）、`max_tokens`、`temperature`、`top_p`、`stop_sequences`、流式与非流式。
- 拒绝：服务端工具（如 `web_search_20250305`）、`document`、`server_tool_use`、各类服务端工具结果、
  工具结果里的 `search_result` 等、`mcp_servers`、`container`。
- 不转发但如实说明：历史中的 `thinking`/`redacted_thinking` 块（签名只对原服务商有效）、
  `thinking`、`context_management`、`output_config`（个人连接推理强度固定 Auto）、`metadata`
  （含设备标识，不发给服务商）。上游的 `reasoning_content` 以无签名的 thinking 块返回给 CLI。
- Claude Code 在转换模式下启动时加 `--disallowedTools WebSearch`（它依赖 Anthropic 服务端工具）。
- `count_tokens`、`/v1/models`、`HEAD /api/hello` 返回 404，CLI 本来就允许它们缺失。

模型锁定：请求里的 `model` 必须等于启动时绑定的模型，否则 400；不会改写成别的模型。

上游流完整性：只有 0 号 choice 以 Chat Completions 定义的 `finish_reason` 结束、且之后没有出错，
才转成客户端的正常结束。`error` 载荷、非对象的 choice/delta、非 0 号 choice、未知或非字符串的
`finish_reason`、形状不对的 `tool_calls`/`content`、读到一半断开或解析失败（即使已经出现过
`finish_reason`）、没有结束就断流，一律转成客户端格式的失败（Responses 的 `response.failed` 加
`error`，Messages 的 `error` 事件），绝不当成成功。读到 `[DONE]` 立即停止并关闭上游请求，不等
上游关连接。

## 四、进程与生命周期

```
setsid（已有包装） ──> python3 -I model-protocol-translator.pyz <CLI argv>   （常驻进程 PID 不变）
                         ├─ 绑定 127.0.0.1:随机端口，监听
                         ├─ fork：子进程 = 本地兼容服务（同一成员 UID、同一进程组）
                         │      PR_SET_PDEATHSIG + 每秒检查父进程，CLI 退出它就退出
                         └─ exec CLI：占位符换成 http://127.0.0.1:<端口> 和成员令牌，
                                       删掉全部 AGENT_COMPAT_* 变量（包括上游 key）
```

- 启动器在常驻 CLI 前面，以同一个成员 UID 运行。`exec` 之后 pidfile 里的 PID 就是 CLI 自己，
  已有的进程组终止、成员进程回收（`member-process-reaper` 按 UID 清理，每次启动常驻前也会先回收）、
  容器停止都会带走兼容服务子进程。没有单独的守护进程，也没有跨回合存活的孤儿监听。
- Claude Code 驱动按 `--model` 的位置固定模型（`argv.index("--model")`），启动器前缀不影响它；
  启动器也从这个参数取锁定的模型。
- 常驻进程能否复用由启动字段（含 `inference_client_protocol`、`inference_local_token` 和
  `inference_source`）决定；停用、删除、轮换、被取代、取消和回收时 CLI 被关闭，兼容服务随之退出。
  原生 Fork 给源成员和新成员各发一份启动字段，各自带自己的令牌，新成员有自己的兼容服务。
- 取消：请求一发出，就有一个线程盯着 CLI 这一侧的连接；CLI 关闭请求（回合取消）时立刻关闭上游
  套接字，不管上游是还没返回响应头、正在用分块编码持续输出，还是以关闭连接结束。
- 保留容器的更新：主机在每次启动转换模式成员前，把由本仓库源码确定性打包的 zipapp（只含
  `cloud_agent_host/protocol_translator.py`、`cloud_agent_shared/protocol_compat/` 及两个包的
  `__init__.py`、许可证和 NOTICE）按 SHA-256
  校验后以 root 拥有、0444 权限装到 `/usr/local/libexec/model-protocol-translator.pyz`（与 MCP
  适配器共用 `trusted_files.install_trusted_file`），不依赖重建镜像。

## 五、凭据与网络

- 上游 key 仍按原有方式加密保存、只写、按所有者读取，claim/常驻时交给主机，runner 在持久输出里
  屏蔽，按沙箱组记录扫描副本。转换模式下 key 只交给启动器（环境变量，不进 argv），`exec` CLI 前
  从环境中删掉；CLI 拿到的是本地令牌。
- 本地令牌由控制面签发：`translator_token` = 连接密钥所用 `CredentialCipher` 的平台 MAC
  （`mac`，HMAC-SHA256，密钥由平台的 `USER_CREDENTIAL_ENCRYPTION_KEY` 按域派生，从不下发）计算
  `"cloud-agent/model-protocol-translation/token/v1" ‖ 所有者 ‖ 成员线程 ID ‖ <连接>@<版本> ‖
  客户端协议 ‖ 模型`（以 0 字节分隔）的十六进制。它不是上游 key 的函数：同一沙箱组里用同一个 key
  的 Fork 兄弟成员知道 key 和对方的线程 ID 也算不出对方的令牌。令牌随转换模式的 claim
  （`ModelConnectionAccess.local_token`，直连 claim 不含此字段）和 Fork 的每个成员启动字段下发，
  经 `SharedResidentLaunchConfig.inference_local_token` 到主机，再经 `AGENT_COMPAT_TOKEN` 交给启动器；
  这些字段都不进 repr。确定性：同一成员进程的每次 claim 和 Fork 算出同一个令牌，runner 把 claim 里的
  令牌和 key 一起加入屏蔽表（`claim_secret_values` → `ModelConnectionAccess.secrets()`）。令牌是进程
  复用身份的一部分，所以平台密钥轮换或任何输入变化都会重启进程，常驻进程不会输出本回合屏蔽表里
  没有的令牌。不新增持久化。令牌只由启动器写进 CLI 的环境变量，argv 里出现令牌占位符会拒绝启动。
- 兼容服务只认 `Authorization: Bearer <令牌>` 或 `x-api-key: <令牌>`（常量时间比较），其他请求
  一律 401；只服务客户端自己的那一个路径，没有健康检查或配置查询端点。
- 只转发到启动时绑定的 `<Base URL>/chat/completions`：HTTPS、系统证书校验、不读环境代理、不跟随
  重定向、调用方不能指定地址或 key。沙箱原有的 nftables 出站规则不变，兼容服务和 CLI 走同一条
  出站路径，不需要额外的主机网络权限，也没有控制面代理。测试用的假上游只能由测试代码在进程内替换
  连接工厂（`protocol_translator.UPSTREAM_CONNECTION`），没有可配置的环境变量或启动参数。
- 上游错误只把状态码映射成客户端格式的错误，不回显上游正文；日志只写类别，不写正文、key 或令牌。

## 六、测试按钮

测试按钮的「API」选项：连接声明的原生协议，加上可用的转换路径（「Responses via Chat
Completions」「Anthropic Messages via Chat Completions」）。原生优先：连接声明了 Responses 时，
Responses 测试走原生，不走转换。转换测试在控制面用**同一份** `protocol_compat` 代码（gate、
namespaces、stream）把探测请求转换成 Chat Completions，发到 `/chat/completions`（Bearer 认证），
再把上游流转换回客户端协议，用原有的 Responses/Messages 解析器做两步工具调用校验。结果记录
`protocol`（客户端协议）、`upstream_protocol` 和 `mode`，只对该模型和该路径有效。只在用户点击时
运行，保存或打开页面不会触发计费请求。

## 七、验证

- `tests/unit/protocol_compat/`：迁来的 106 个上游翻译器测试；gate（拒绝列表、模型锁定、命名空间
  扁平化与历史、冲突、强制 tool_choice、不转发的字段）；stream（各种异常流、未知或非字符串的
  `finish_reason`、非 0 号 choice、嵌套形状错误、结束后再损坏、命名空间还原、非流式）。
- `tests/unit/test_protocol_translator.py`：本地服务和启动器（令牌与兄弟成员令牌、401/404、
  模型锁定、上游错误不回显、`[DONE]` 后上游不关连接、截断与损坏、延迟响应头和分块保持连接时的取消、非流式；启动器真正 fork/exec、占位符替换、CLI 环境里没有 key、
  CLI 退出或被杀后服务端口关闭）。
- `tests/unit/test_model_protocol_compat_launch.py`：适配器 argv/env、URL 类型、启动字段和进程复用、
  启动字段带令牌、复用身份包含令牌、claim 屏蔽令牌、zipapp 确定性与可运行（只含标准库和纯包的
  导入）、保留容器的刷新。
- `tests/unit/test_model_connection_compat_check.py` 和服务端测试：转换测试的 URL、认证和两步工具
  调用，未完成的回答不算通过；准入、领取（能力门槛、令牌依赖平台密钥而不依赖上游 key、兄弟成员
  不同）、Fork 的两个成员各自的令牌与各自后续 claim 一致、后续消息、`/config`、API 测试路由的原生优先。
- `tests/external/test_model_protocol_compat_cli.py`：固定版本的 Codex 0.145.0（`app-server`，与
  生产驱动相同的 RPC）和 Claude Code 2.1.220（常驻 stream-json）经由真实打包的启动器和本地假 Chat
  上游：上游只收到 Chat 请求和上游 key；工具（`exec_command`/`Bash`）真的执行，结果进入下一次 Chat
  请求；CLI 正常结束回合；CLI 环境里只有成员令牌；用同一 key 的兄弟成员由控制面签发的令牌、以及
  上游 key 本身都被拒绝；CLI 退出后服务消失。
  另一用例让模型调用扁平化的 `multi_agent_v1__close_agent`，Codex 按自己的 `close_agent` 处理
  （返回「invalid agent id」），证明命名空间还原有效。
- 前端：设置页显示经转换的 Agent 和转换测试选项；输入框在 Chat-only 连接上为 Codex/Claude Code
  显示「via Chat Completions」，多协议连接对原生 Agent 不显示；桌面与手机宽度浏览器测试。

不做：真实服务商、Kata 主机上的端到端验收（需要真实 key 和主机，交由后续验收记录）。
