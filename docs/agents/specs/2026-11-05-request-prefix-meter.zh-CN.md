# 请求前缀计量：测量 system prompt 和工具定义

- **日期**：2026-11-05
- **状态**：已实现（本分支 `murphy/context/request-meter`）

## 问题

Context 面板的 Token Stats 圆环和 Context Browser 里，「系统提示词」和「工具定义」两项一直显示 not recorded。原因是：

- 平台不保存这两样东西的原文；
- 没有任何 CLI 在自己的输出里报告它们的大小；
- Gateway 删除后，平台不再处在模型请求的路径上（[决定](2026-10-30-independent-accounts-only.md)）。

每个请求都会带上它们，而且常常占上下文的很大一部分（MCP 工具多的时候尤其明显），但面板一直给不出数字。

## 约束

- **不恢复网关层。** 2026-11-05 确认平台不做 token 额度，配额和计费仍归模型厂商。为观测性单独重建一层网关不值得。
- **不碰对话通道。** 不向 CLI 的 stdin 问 `/context`（理由见 [context snapshot](../../../apps/backend/cloud_agent_host/runtimes/context_snapshot.py) 的模块说明）。
- **不用相减推算。** 不用「厂商报的输入 − 平台记录的内容」去估算差值（`context_request.py` 的规则）。
- **计量只是诊断。** 它不能让请求变慢、失败或被拦下。

## 设计

### 在哪里测：成员自己的 loopback 服务

翻译计划（Codex / Claude Code 跑在 Chat Completions 连接上）原本就在 CLI 前面放了一个成员私有的 loopback 服务（`protocol_translator`），它读得到完整的请求体。这次让它有两种模式：

| 计划 | 服务模式 | 做什么 |
|---|---|---|
| 翻译 | translated（不变） | 翻译请求；同时测量 CLI 发出的请求体 |
| 直连 | **passthrough**（新增，`model_passthrough.py`） | 原样转发；同时测量 |

passthrough 的要点：

- **绑定一个上游 origin。** launcher 只拿到 `https://<authority>`，没有 key、token 或 model。CLI 的 URL 改成 `http://127.0.0.1:<port>/<authority><原路径>`；服务只接受这个前缀下的路径，把剩下的部分转发到 origin。
- **主机名留在 URL 里。** pi 这类 CLI 会用 URL 文本识别厂商（比如 `deepseek.com`），所以 authority 要出现在 CLI 看到的 URL 里，这样识别结果不变。
- **原样转发。** 请求和响应都原样传递，只处理逐跳头、`Host` 和 body 的分帧（chunked 请求会改成带 Content-Length 转发；没有长度的响应改成 chunked 回传）。厂商的错误连同错误体原样交给 CLI，因为 CLI 要靠它们决定重试和压缩。
- **响应断在半路，就让 CLI 立刻看到。** 厂商声明了长度却没发完就断开时，服务立即关闭和 CLI 的连接，CLI 会像直连时一样报错并重试，而不是一直等到超时。
- **重定向。** 3xx 交给 CLI 自己处理。`Location` 如果是 origin 上的路径（如 `/v2/messages`），会补回 `/<authority>` 前缀，否则 CLI 会把它解析到 loopback 的根路径，结果变成 404。完整 URL 和相对引用原样传递。
- **复用上游连接。** 每个 CLI 连接保持自己的一条上游连接，和 CLI 直连时一样。空闲期间被厂商关掉的连接，会在使用前检测出来并换新。
- **先转发，后测量。** 请求体发出去以后才解析测量，正好和厂商处理请求的时间重叠。解析失败或 body 太大（> 16 MiB）就不测，但照常转发。
- **启动失败不挡 turn。** fork 出的服务建好 server 后，通过一个管道告诉 launcher「已就绪」，launcher 收到后才把 CLI 指向 loopback。服务起不来（bind 失败、子进程初始化失败、10 秒内没报就绪）时，launcher 把占位符换回原始 URL，让 CLI 直接连厂商。翻译模式没法直连，这种情况下启动直接报错，CLI 不会被指向一个不存在的端口。等待就绪大约多花十几毫秒（加载系统信任库的时间）。

### 哪些直连不计量

Claude Code 连 `api.anthropic.com` 时不计量。Claude Code 会按 base URL 的**主机名**判断是不是官方 API，比如非官方主机默认关闭 MCP tool search。只要它看到的不是 `api.anthropic.com`，行为就可能变化。规则写在 `runtimes/base.py` 的 `FIRST_PARTY_HOSTS` 里。其他组合在计量前后看到的都是非官方主机，或者靠 URL 文本识别厂商（路径里的 authority 会保留这类识别），所以照常计量。

### 测什么：`cloud_agent_shared/request_prefix.py`

每个请求只记大小，不记原文：

- **system prompt**：放在协议规定位置的那部分，对话里出现的不算。
  - Anthropic：`system`
  - Responses：`instructions`，再加上 `input` 开头连续的 system/developer 消息
  - Chat Completions：`messages` 开头连续的 system/developer 消息
- **工具定义**：`tools` 里的每一项，按发送时的紧凑 JSON 计算。
- 每部分都记字符数，以及 ASCII 和 CJK 字符数，也就是 `chars_cjk_v1` 估算的输入。每个工具单独记名字和大小，只保留最大的 200 个，其余计数。
- 前缀指纹：对 system 和 tools 原值做 sha256。

同一个服务进程里，某个指纹第一次出现时写完整记录，之后只写一行「重复」（`{"fp", "repeat": true}`）。

写 stderr 不能卡住请求：请求线程只把记录放进一个有界队列（64 条），由单独的线程写出。队列满（比如 stderr 管道没人读）时丢掉这条记录，计量器同时忘掉这个指纹，下次再遇到时重新写完整记录，免得 host 收到一条无法还原的「重复」。

### 怎么报出来

记录写在 CLI 的 **stderr** 上，行首是 `\x1emodel-request-prefix `。只能走 stderr：stdout 被 CLI 的协议占用，成员进程又是 `docker exec` 起的，只有标准输入输出这几条流。

- **host 端拆分**（`request_prefixes.PrefixRecords`，由 `SandboxProcess.sieve_stderr` 调用）：把记录从诊断用的 stderr 尾巴里拿出来，免得把真正的报错挤掉；记录要逐项校验，并且有大小上限。重复行按指纹还原成完整记录。
- **按 turn 上报**（`TurnPrefixes`，在 `ResidentTurn.lines()` 里）：每个 turn 里每种前缀只发一次 `lifecycle` 事件，`phase: "request_prefix"`。这样每个 attempt 都知道自己的请求带了什么，不管 CLI 第一次发这个前缀是多久以前。
- 记录是 CLI 进程 stderr 上的文字，CLI 或同组成员都可以伪造。所以它只用来展示给 owner 看，平台不会依据它做任何决定。

### 控制面怎么用

- **profile reducer**（`context_profile.py`，版本升到 6）：收集每个 attempt 里不同的前缀，最多 20 个，记下 system/tools 的估算 token 数。`main_prefix()` 选**第一个带工具的前缀**：一个 turn 总是从主 agent 回答 prompt 开始，生成标题、摘要这类旁路请求不带工具，子 agent 的请求出现在后面。
- **Token Stats**（`context_dashboard.session_tokens`）：取最近一个测到主前缀的 turn，把 `system_prompt` 和 `tool_schemas` 放进 `recorded`；只有确实没测到的分类才留在 `not_recorded` 里。
- **Context Browser**（`RequestComposition.prefix`）：显示该请求所在 attempt 的主前缀，工具可以展开，按大小列出。前端组件是 `PrefixRows.tsx`。

### 兼容性和上线

- host 比控制面先上线。旧控制面不校验生命周期事件的 phase，会照常存下新事件；前端会隐藏不认识的 phase。
- 计量 helper 和翻译 helper 是同一个 zipapp，现在每个容器只装一次（`install_once`，key 带内容 digest）。所以直连成员启动时，除了容器里的第一次，不会多出 exec。
- 旧会话在新 host 上的第一个 turn 之前，仍然显示 not recorded。

## 没有做的

- **计时**（TTFT、生成时间）：passthrough 已经在路径上，测起来很容易，但要把数据接进 Timing Stats，需要按请求做关联，留给后续。
- **精确关联到每个请求**：Claude Code 的 `usage_id` 可以和响应里的 message id 对上，但这次只按 attempt 选主前缀。
- **开关**：没有加 host 设置项。出问题时的处理办法是：计量服务启动失败会自动直连；系统性问题就回滚 host 版本。
