# 用户自带模型连接（BYOK）设计与实现记录

日期：2026-10-27
状态：已实现（含 Anthropic Messages 与 Claude Code），等待合并；部署开关默认关闭，尚未上线，也未用真实服务商做端到端验收

## 一、要解决的问题

到目前为止，沙箱里的 Agent CLI 只能通过 Gateway 调模型：每个成员进程拿到一张由 Gateway
签发、按用户和模型限定的短期 grant，花的是 Gateway 给这个用户的额度。有些用户手里有自己的
OpenAI 或 Anthropic 接口的服务（OpenAI、Anthropic、OpenRouter、DeepSeek、公司内部对外开放的推理
服务等，HybridInference 也可以是其中一个上游），希望任务直接用自己的 key 跑，账单记在自己头上。

本次做的是**用户自己的推理连接**，不是公开推理 API，也不是管理员出钱的共享 key：

- 连接只属于创建它的用户，不随团队共享，Fork 也不会把它带给别人；
- key 加密存储，对用户接口只写不读；执行所需的凭据只通过受认证的内部通道交给对应成员；
- 上游费用由 key 的主人承担，平台不从 Gateway 推理额度扣费，也不代设上游预算；
  已有运行时 token 统计仍可显示，但不将未知上游费用显示成零；
- 登录和「能不能用 Agent」（Agent admission）照旧走 Gateway。独立账号、排队名单、平台额度不在
  这次范围内。

## 二、范围

**做了的：**

1. 个人设置页「模型连接」：新增、编辑、轮换 key、停用/启用、删除、从 `/models` 读取模型列表；新增和改动端点或模型时先验证再保存。
2. 每个连接：显示名、Base URL、API key、模型 ID（最多 8 个）。`/models` 只是辅助填写，没有它也能
   手填。用户不需要知道服务商提供哪种 API，也不能手动声明：每个模型支持哪些 API 在保存前由验证
   自动识别（见第十三节）。
3. 新任务、后续消息、排队消息、目标（goal）自动轮次、原生 Fork 都能用连接上的模型，并且记住
   「是哪个来源的哪个模型」。
4. 「验证并保存」对每个要保存的模型在三种 API 上各真跑一次流式推理加一次工具调用往返，每种 API
   的结果分成「支持 / 不支持 / 失败」三类，按模型保存；有模型没有任何 Agent 能用就什么都不保存。
5. 停用、删除、轮换 key 会让旧进程退出，新回合不会再用旧 key。

**没做的（有意不做）：**

- 通用协议转换器、路由或计费网关、key 池，以及推理时的试错回退。平台只用某个模型验证可用的
  API；除[协议兼容设计](2026-10-28-model-protocol-compatibility.zh-CN.md)里的两种
  转换外，模型不提供某个 CLI 的协议，就不能给那个 CLI 用。不引入 HybridInference 的服务端路由模块。
- 连接模型的推理强度（reasoning effort）。没有可靠的模型能力表，只允许 Auto；带强度会被拒绝，
  从 Gateway 模型切过来时也会自动清掉。
- 在「我的设置」里把连接模型设为默认（pin）。pin 仍只针对 Gateway 模型；「沿用上次」会记住来源，
  见第五节。
- 连接任务的标题生成。标题生成用的是 Gateway grant，连接任务保留首行文字作为标题。
- 团队共享连接、管理员代付、私网地址例外。

## 三、协议矩阵

| 运行时 | 适配器说的协议 | 该模型需要可用的 API | 说明 |
|---|---|---|---|
| OpenCode | Chat Completions | `chat_completions` | 由镜像里的包装脚本写入 provider 配置 |
| Kilo | Chat Completions | `chat_completions` | 同上 |
| pi | Chat Completions（`openai-completions`） | `chat_completions` | 同上 |
| Codex | Responses | `responses` | provider 的 `wire_api` 固定为 `responses` |
| Claude Code | Anthropic Messages | `anthropic_messages` | `ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`，见下 |

这张表放在共享词汇 `cloud_agent_shared/model_connections.py`（`RUNTIME_PROTOCOLS`），控制面和
主机都从这里读。判断「这个回合能不能在这个连接上跑」只有一个函数：
`domain/model_connections.require_usable`。它依次检查：连接存在且属于本人、未删除、已启用、
运行时有对应协议、模型在列表里、该模型在当前密钥和 Base URL 下有验证结果、
验证结果给这个运行时找到了路径（原生优先，否则走转换）、没有设推理强度。每种拒绝都有固定的原因码和
给用户看的句子，任何一种都不会退回 Gateway 或别的模型。

Base URL 原样使用，不像 Gateway 中继地址那样补 `/v1`：有的服务商路径是 `/api/v1`、
`/v1beta/openai`。CLI 自己在后面拼 `/chat/completions` 或 `/responses`。

Anthropic Messages 的约定不同：Claude Code 的 SDK 会在 `ANTHROPIC_BASE_URL` 后面拼
`/v1/messages`（本地 CLI 2.1.311 与项目固定版本 2.1.220 均已通过模拟服务核对：请求
`/v1/messages?beta=true`，带 `x-api-key` 和 `anthropic-version: 2023-06-01`；该验证不代表
真实模型推理或 Kata 端到端验收）。所以对这一协议只去掉 Base URL
结尾的**一个** `/v1`，其他路径原样保留（`anthropic_base_url`）：`https://host`、`https://host/v1`、
`https://host/anthropic`、`https://host/anthropic/v1` 都到 `…/v1/messages`；`/v1beta` 或中间的
`/v1` 不动。这样一个以 `/v1` 结尾的 Base URL 可以同时服务三种协议。验证用同一个函数算 URL，
保证验证和真实 CLI 打到同一个端点；不加中继，也不随意截取或重拼路径。

## 四、数据和来源

迁移 `0106_user_model_connections`：

- `agent_model_connections`：`id`（`mconn_` + 32 位十六进制）、`user_id`、`name`、`base_url`、
  `models`（最多 8 个）、`api_key_ciphertext`、
  `credential_revision`、`config_revision`、`enabled`、`model_checks`（按模型 ID 保存的验证结果，见
  第十二节）、`deleted_at`。同一用户未删除的连接名字不能重复（不区分大小写）。
- `agent_model_connection_checks`：验证和读取模型列表的次数，按所有者计，用于每小时限额；连接
  还不存在时（新建前的验证）也计入。
- `agent_model_connection_scan_keys`：仅保存确实绑定到成员常驻进程的密钥版本，以沙箱组、连接和
  凭据版本为范围加密保留，用于该组后续快照与受信发布的秘密扫描。新建但未使用的连接不会写入。
- `agent_jobs.model_connection_id`、`agent_pending_messages.model_connection_id`：NULL 表示
  Gateway。**不建外键**，删除连接不能把回合的来源改成 NULL，否则它就悄悄变成了 Gateway。
- `agent_model_connection_residencies(thread_id, residency_generation, connection_id,
  credential_revision)`：当前常驻进程是用哪个连接的哪个版本启动的。单独成表而不是成员表的新列，
  这样运行中的上一个版本对成员表的预编译 `SELECT m.*` 不会因列变化失效；关联时必须匹配当前的
  常驻代，旧连接的轮换不会误关更新的 Gateway 常驻。

来源规则：

- 「模型」永远和「来源」一起出现。请求里给了 `model`，`model_connection_id` 就是它的来源，
  不给或为 null 就是 Gateway。不给 `model` 时，模型和来源一起继承上一回合。只给连接不给模型是
  非法请求。
- 同一个模型 ID 可以同时出现在 Gateway 和多个连接里，界面上是不同的选项，存下来也不会混。
- 连接上的模型 ID 按服务商原样保存，不经过 Gateway 的别名解析。
- 排队消息保存发送时的来源；真正发出时再按当时的最新回合重新检查一遍，连接已停用或删除就把
  消息挂起，并写明原因。
- goal 自动轮次和原生 Fork 复制上一回合的来源；Fork 只能 Fork 自己的会话，所以不会出现
  跨用户使用连接。

## 五、默认模型

`/config` 新增 `model_connections`（当前用户已启用的连接、模型和可用运行时，不含 key）和
`default_model_connection_id`。默认值的顺序不变：pin（Gateway 模型）> 上次用的模型 >
部署默认值。区别在于「上次用的模型」带着来源：上次是在连接上跑的，就只在那个连接仍启用且仍列着
这个模型时预选它，**绝不**拿 Gateway 上的同名模型顶替。Gateway 目录读不到时，连接模型照样可选。

`/v1/agent/model-preference` 同时返回 `last_model_connection_id`。上次在连接上运行、又没有 pin 时，
`effective_model` 为 null（pin 和部署默认值都是 Gateway 模型），设置页据此说明「新任务会再次提供
这个连接上的模型」，而不是把它说成 Gateway 默认值。个人连接上的显式 pin 本版不做。

## 五之一、前端

- 选择项始终是「模型 + 来源」二元组。草稿、GitHub 往返回执、请求和 API 状态都分别保存原始
  `model` 与 `model_connection_id`；只有选择器内部用编码后的键区分同名选项：Gateway 模型的键就是
  模型名本身，个人来源的键是 `["mconn_…", model]` 的 JSON，以 `[` 开头的 Gateway 名字也编码，
  两者不会相撞。旧草稿里的模型字符串一律按 Gateway 读，不当作编码键解析。
- 一个模型只出现在它当前能运行的 Agent 下，依据是该模型自己的验证结果（`/config` 的
  `model_runtimes`），与执行时的检查完全一致；没有当前验证结果的模型（兜底，正常不会出现）不出现。标签只写模型和
  连接名，不写协议，也不写「经 Chat Completions」。
  个人来源的推理强度只有 Auto，请求里不带 `reasoning_effort`。
- 草稿或「上次使用」指向的个人来源当前不能运行（换了 Agent、连接停用/删除、不再列出该模型）时，
  选择器仍显示它并标注「不可用」，运行按钮保持禁用并提示原因；不会退到默认模型或同名 Gateway 模型。
  后续消息继承的来源不可用时同样禁用发送。
- 后续消息（主会话与侧边会话）把模型和来源一起与上一回合比较：只换来源、模型名相同，也会同时发送
  `model` 和 `model_connection_id`；换回 Gateway 时只发送 `model`。继承来源取 API 最新回合，没有更新
  回合时取当前页面的回合。排队消息的编辑只改正文，不发送 `model`，服务端保留原来源；
  `model_connection_id` 不能和空模型一起提交。
- 提示词改写走 Gateway 推理，选中个人来源时禁用并说明原因，绝不把个人来源的模型发给 Gateway。
- 用量：`JobUsageResponse.external_provider` 为真时，回合用量只显示运行时上报的 token，并说明费用由
  服务商计费；不向 Gateway 请求刷新，也不显示 $0 或一直等待。会话合计在含个人来源回合时标记
  `external_provider` 且 `incomplete`，Gateway 小计不包含这些回合。管理员运行详情只显示
  「由所有者的服务商计费」，不暴露连接 ID。所有者与管理员的跨任务用量汇总仍把这类回合记为
  Gateway 用量 `unavailable` 的运行，不计为零费用，也不单独标注服务商。

## 六、凭据生命周期

1. **写入。** 创建或轮换时，key 先用 `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` 在独立的
   `hybridinference/model-connection-key/v1` 域下加密再入库。接口只返回 `has_api_key`。
   校验失败的 422 只说哪个字段错了，不回显提交的值；所有响应都是 `no-store`。
2. **认领（claim）。** 每次认领都按任务的所有者重新读取连接并调用 `require_usable`。主机没有声明
   `model-connections-v1` 能力，或者部署读不出 key，认领退回队列，不消耗尝试次数；连接已停用、
   删除或不再适用，任务直接以对应原因失败。连接任务不向 Gateway 申请 attempt grant。认领响应里的
   `model_connection` 带 key，runner 把它加入输出脱敏表。
3. **常驻（residency）。** 成员进程启动前，控制面再查一次连接，然后在连接行的锁下把
   「连接 + 版本」写到成员行上，再把 key 作为这个成员的凭据返回；不签发 Gateway grant。
   如果此刻连接刚被改过，写入会失败，常驻被释放，任务以「连接在启动过程中发生变化」失败。
   runner 还会核对返回的版本和认领时的一致。
4. **进程。** key 只进成员自己的进程环境变量和激活 tmpfs 里的 CLI 配置，不进命令行参数，
   不进共享工作区。进程启动参数里固定了来源 `连接@版本`，所以下一回合如果连接被轮换过，
   就不会复用旧进程，而是带着新 key 原生恢复（native resume）。从 Gateway 切到连接或反过来，
   同样会换进程。
5. **失效。** 经过验证的新 key 或新 Base URL、停用、删除都会提升 `credential_revision`（删除还会
   抹掉密文，只留墓碑）。是否「改了」由仓库在 `UPDATE` 里和库中已存值比较决定，提交的值与原值相同
   不算改动；重新填入同一个 key 也会被接口识别并忽略。所以重命名或移除模型不会提升版本、不会关闭
   进程，也不清掉其他模型的验证结果。提交后立即释放所有绑定在旧状态上的常驻，并通过主机关闭对应进程，走的是删除
   会话时同一条路径。正在跑的回合会因此中断；主机联系不上时，下一次常驻心跳会被拒绝，进程照样退出。
   因为绑定和失效在连接行上互斥，「先绑定后失效」会被找到并关闭，「先失效后绑定」会被拒绝。
6. **账号切换。** 每个用户的会话、成员 UID、私有家目录和 tmpfs 本来就是分开的，连接查询全部按
   所有者过滤，别人的连接 ID 一律当作不存在。

## 七、信任边界（有意为之）

这是**用户自己提供**的推理凭据，和「平台持有的长期凭据不得进入沙箱」那条规则不冲突，也没有把任何
平台密钥重新归类。但要说清楚它意味着什么：

- 仓库代码、依赖、Agent 本身都能读到这把 key：它就在 CLI 的环境变量里，CLI 启动的命令会继承它，
  CLI 的配置文件也在成员的 tmpfs 里。它们可以把 key 写进文件、模型上下文，或者通过现有的公网出口
  发出去。平台只能做到：加密存储、只写不读、在 runner 的持久输出里屏蔽已知值、通过 Gateway 之外的
  服务商时不复用平台 grant。这些措施防不住有意的外泄。
- 同一沙箱组里的其他成员读不到它（不同 UID、私有 tmpfs、不进参数），但和环境变量一样，成员共享
  可写工作区，别人种下的 Git 钩子或构建脚本会以执行者的身份运行，从而能读到执行者的 key。
- 受信的快照和发布路径扫描该沙箱组实际使用过的密钥，包括同组 Fork 成员、轮换前版本及已删除
  连接的已用版本。扫描副本使用独立加密域，执行路径不会选择这些副本；解密后仍是真实上游密钥，
  不能把加密域隔离解释成密钥本身失去推理权限。副本随沙箱组记录保留，不把同一用户其他沙箱的
  密钥送入本组；超过扫描数量上限时拒绝扫描，不能静默漏掉旧版本。
- 扫描和输出脱敏不能防止主动外泄或沙箱内直接 Git 推送。删除连接只阻止平台继续使用它，不能
  撤销服务商处的密钥；出现泄露应在服务商处撤销或轮换。
- 网络边界不变：沙箱只能访问公网 IPv4 的 TCP 80/443，连接的 Base URL 必须是公网可达的
  HTTPS（443 端口）地址。没有新增私网例外，也没有代理。

## 八、服务端验证的网络防护

「验证并保存」会让控制面去拨用户填的地址，所以沿用 MCP 连接的防护：

- 只接受 `https`、443 端口、主机名或 IPv4（不接受 IPv6 字面量），URL 里不能带用户名密码、
  查询串或片段；
- 解析出的**每一个**地址都要通过 MCP 的公网策略（拒绝回环、私网、链路本地/元数据、CGNAT、组播、
  保留段、文档段）以及沙箱的受保护网段（含 `AGENT_EGRESS_PROTECTED_CIDRS`），且至少要有一个
  IPv4；
- 实际连接被钉在刚检查过的 IPv4 地址上，防 DNS 重绑定；不跟随重定向，不读环境里的代理设置；
- 上游返回的正文一律不回显，只保留 HTTP 状态码和从流里解析出的事实；读取有大小上限；
- 每个用户每小时最多 20 次（验证一个模型算一次，读取模型列表也算一次），在数据库中原子预留次数
  以覆盖多副本并发；一次保存需要的次数一次性全部预留，预留之后才发起网络请求，限制的是次数而不是
  并发。调用期间不持有数据库事务。

每种 API 的检查流程：先发一个带一个工具的流式请求，要求模型调用它；再把工具结果带回去发第二个流式请求，
要求模型给出包含本次工具结果的最终回答。第一步必须恰好有一次调用、且调用的是提供的工具；第二步不能再请求任何工具。
校验调用 ID、工具参数、预期结果和完整终止事件，
Responses 第二步保留第一步输出上下文。Anthropic Messages 用原生格式：`x-api-key`、
`anthropic-version`、`tool_use`/`tool_result` 内容块，第二步原样带回助手内容（含带签名的 thinking）；
流必须依次有 `message_start`、每个内容块的开始/增量/结束、带正常停止原因的 `message_delta` 和
`message_stop`，`error` 事件、块类型与增量不符、`message_delta` 之后还有内容、`max_tokens`/`refusal`
等停止原因、未关闭的块都不算通过（`application/model_connection_anthropic.py`）。读取模型列表时
先试 OpenAI 风格的 `GET /models`，该列表不存在或被拒绝时再用同样的认证头试 `GET …/v1/models`。
两步都成功才是该模型在该 API 上「支持」，不代表同一连接上的其他模型，也不等于真实 CLI 端到端验收。服务商能连上但做不到其中一步（404、400/422、
不是流、没调工具、没给最终回答）是「不支持」；连不上、证书错、重定向、401/403、429、5xx、
回答过大是「失败」。单纯 `GET /models` 成功不会显示为通过。

## 九、主机侧改动

- 认领载荷新增 `model_connection`，runner 解析后加入脱敏表。
- `ResidentLaunch` 和沙箱管理器的启动配置新增 `inference_base_url`、`inference_protocol`、
  `inference_source`，并参与「能否复用进程」的判断。沙箱管理器只替换 Gateway 和 MCP 中继地址，
  连接的 Base URL 原样保留。
- 运行时适配器用 `ExactBaseUrl` 类型区分「原样使用的地址」和「需要规范化的 Gateway 地址」，
  这样不用改动每个驱动的参数。地址按协议分类型：Anthropic Messages 连接是 `MessagesBaseUrl`。
  OpenAI 协议的连接只设置 `OPENAI_*`，不设置 `ANTHROPIC_*`；Codex 改 provider 的 `base_url`；
  Claude Code 只接受 `MessagesBaseUrl`，设置 `ANTHROPIC_BASE_URL`（去掉结尾一个 `/v1`）和
  `ANTHROPIC_API_KEY`，不设 `ANTHROPIC_AUTH_TOKEN` 和任何 `OPENAI_*`；MCP 中继令牌仍是单独的变量。
  OpenAI 适配器拿到 `MessagesBaseUrl`、Claude Code 拿到其他连接地址都直接拒绝。
- `credential` 和 `mcp_credential` 不再出现在启动配置的 repr 里。

## 十、上线顺序

1. 保持 `AGENT_MODEL_CONNECTIONS_ENABLED` 关闭（默认值）。先升级主机（runner + 沙箱管理器）。
   新主机声明 `model-connections-v1`；旧主机不声明，
   连接任务不会被它认领。
2. 迁移数据库并升级全部控制面副本，然后才启用个人连接。迁移虽是新增结构，旧控制面却不知道
   来源列；有个人来源数据后，新旧控制面混用可能把个人来源解释成 Gateway，不能据此宣称兼容。
3. 有个人连接或个人来源历史时，禁止直接回退旧控制面；完成的会话也可能被恢复或 Fork。downgrade
   会拒绝这些数据，不仅检查运行中任务。关闭功能只停止新使用，不让历史来源消失。
4. 配置 `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` 并保留可恢复的备份。设置页只说明功能是否可用，
   运维配置细节不放入普通用户流程。

## 十一、验证

自动化测试（不需要真实服务商的 key，上游全部是进程内假服务）：

- `tests/unit/test_model_connections_domain.py`：URL/模型/协议的规范化和拒绝，`require_usable`
  的每种拒绝原因，地址防护（私网、元数据、CGNAT、IPv4 映射、纯 IPv6、受保护网段），两种协议下的
  流式工具调用往返，各类失败与不支持的区分，重定向不跟随，上游正文里的 key 不回显，模型列表读取。
- `tests/unit/test_model_connection_launch.py`：认领载荷与脱敏，常驻版本核对，进程复用判断
  （轮换、换地址、来源切换都不复用），沙箱管理器保留连接地址，各运行时拿到的确切 URL 与 key
  （不进参数、不设其他协议的变量），Claude Code 拒绝不提供 Messages 的连接。
- `tests/unit/test_model_connection_anthropic_launch.py`：Claude Code 在各种 Base URL 形状下的
  `ANTHROPIC_BASE_URL` 与测试 URL 一致，key 只在环境变量里，推理强度为 Auto，OpenAI 适配器拒绝
  Messages 连接，轮换或协议变化不复用进程。
- `tests/unit/test_model_connection_anthropic_check.py` 与审查回归
  `tests/unit/test_model_connection_anthropic_review.py`：原生 SSE 往返、URL 与认证头、401/403/404/
  429/5xx、非流式、截断、`max_tokens`/`refusal`、错误事件、帧顺序、调用 ID 与参数、结果关联、
  带签名 thinking 的回传、Messages 专用的模型列表读取。
- `tests/servers/test_model_connections_api.py`：只存密文、不返回 key、422 不回显、跨用户不可见
  不可改不可测、轮换/停用/删除触发失效、测试限额；新任务不读 Gateway 目录、各种不适用组合在入队前
  拒绝；后续消息继承并复查来源、切换来源时的检查、排队消息遇到已删除连接被挂起；认领带 key 且
  不申请 Gateway grant、无能力主机退回、不适用时任务失败；常驻拿到绑定版本的 key、连接中途变化时
  释放并失败；`/config` 在 Gateway 目录不可用时仍提供连接和上次的连接默认值。
- `tests/integration/storage/test_model_connection_store.py`（真实 PostgreSQL）：所有者隔离、
  名字唯一、版本提升规则、墓碑不可复活、回合与后续消息的来源继承与切换、排队消息的来源、
  常驻绑定只认本人当前版本、新常驻代清空绑定、轮换/停用/删除后释放旧常驻且他人无法释放。
- 前端（vitest）：`ModelConnectionsSettings.test.tsx`（设置页增删改测与错误显示）、
  `modelSource.test.ts`（同名不同来源的身份、按模型可用的 Agent 过滤、旧草稿、不可用标注、来源切换的请求字段）、
  `TaskComposer.modelSource.test.tsx`（提交来源、无推理强度、草稿与 GitHub 回执保留来源、不可用时
  禁止运行且不退回 Gateway、改写禁用）、`JobDetail.modelSource.test.tsx`（继承、同名换源、最新回合
  与当前回合两条继承路径、继承来源不可用时禁止发送）、`SideChat.test.tsx`、
  `PendingMessageQueue.test.tsx`（只改正文保留来源）、`LedgerUsage.test.tsx`、`RunDetails.test.tsx`、
  管理员运行详情、`ModelSettings.test.tsx`。
- 浏览器（Playwright）：`e2e/model-connection-verification.spec.ts`（见第十三节）、
  `e2e/model-connections.spec.ts`（1440px 与 390px 设置流程）和
  `e2e/model-source-attribution.spec.ts`（个人来源在刷新后保留、切换到同名 Gateway 模型、变为不可用）。
- 后端审查回归：`tests/servers/test_model_connection_usage_review.py`、
  `tests/unit/test_model_connection_message_review.py`，以及真实 PostgreSQL 的
  `tests/integration/storage/test_model_connection_attribution_review.py`（管理员查询与会话用量行
  保留来源，包括尚无尝试的回合）。

未覆盖：没有拿真实服务商做端到端验证；Kata 主机上的真实 CLI 走连接也没有在本分支跑过，
需要在 staging 用测试 key 跑一次 OpenCode 和 Codex。

## 十二、自动识别 API（2026-10-28 修订）

用户看了预览后明确表示：不该让他们勾选「支持的 API」，也不该在主表单上把协议和 Agent 绑在一起；
协议的选择和转换应由 Cloud Agent 在后台完成。本节记录这次修订。其中「保存后再逐个验证」「高级里的
手动声明」和单独的 `/verify` 接口已被第十三节取代，按模型存储、失效规则、有界探测和三项评审修复仍然
有效。

**用户看到的。** 新增连接只填名称、Base URL、API key 和模型 ID。只有用户点「保存并验证」或某个
模型上的「验证」时，平台才会调用服务商；打开页面、编辑字段、单纯保存都不会。按钮旁说明验证会发出
少量请求，服务商可能计费。每个模型显示「可用」（并列出能用它的 Agent）、「未验证」、「无法验证」
（附原因，可重试）或「无法用于 Agent」（附原因）。每种 API 的原生结果和每个 Agent 走的路径放在
折叠的「API 详情」里；手动声明 API 放在折叠的「高级」里，默认关闭。

**存储。** `model_checks` 是一个 JSON 对象，键是模型 ID，值是该模型最近一次验证：
`{"revision", "checked_at", "protocols": {<api>: {"status", "category", "http_status"}}}`。
`status` 只有三种：`supported`（流式请求和工具往返都成功）、`unsupported`（服务商有应答但做不到）、
`failed`（无法判断：连不上、密钥被拒、429、5xx、超时等）。没有出现在结果里的 API 视为未知。只保存
上游的原生事实；能否经转换使用由 `domain.model_connections.model_paths` 从这些事实推导，不落库。
失败既不当作支持，也不当作「该 API 不存在」。

**按模型生效。** 结果只在 `revision == config_revision` 时有效。`require_usable` 只看这个模型自己的
结果：未验证或已过期的模型拒绝为 `model_unverified`，验证过但没有给该 Agent 的路径拒绝为
`protocol_unsupported`，提示里写 Agent 而不写协议。一个模型验证通过不会让同一连接的其他模型变得
可用，新加入或改名的模型 ID 从未验证状态开始。准入、后续消息、排队消息、goal 轮次、认领、常驻和
Fork 都调用这个函数，输入框的 `model_runtimes` 也来自同一个 `model_paths`，所以界面提供的和执行时
接受的完全一致。模型 ID 原样保存，从不替换。

**失效与竞态。**

| 实际变化 | `credential_revision`（进程复用、释放常驻） | `config_revision` 与 `model_checks` |
|---|---|---|
| Base URL 或 key | +1 | +1，全部清空 |
| 手动声明或模式 | +1 | 不变（上游事实没变） |
| 停用 | +1 | 不变 |
| 模型列表 | 不变 | 删掉的模型连同其结果一起去掉，新模型没有结果 |
| 名称，或提交了相同的值 | 不变 | 不变 |

记录结果用一条行级 `UPDATE`：`model_checks = model_checks || {model: result}`，条件是未删除、已启用、
`config_revision` 和 `credential_revision` 仍等于预留时读到的值、模型仍在列表里；它从不写 `enabled`
或任何版本号。于是密钥或 Base URL 变更前发出的探测会被丢弃（接口返回 `recorded: false`），验证期间
被停用（即使又启用了）的连接也不会记录；不同模型的并发验证各自合并，互不覆盖。验证结果改变了某个
模型的路径时不提升 `credential_revision`：计划和令牌本来就属于进程复用身份，下一回合会换进程；
认领和常驻之间则由常驻响应里的 `model_connection_plan`（`client>upstream`）与认领比对，路径在这期间
变了就让这次启动失败，不会只关闭同一连接上其他模型的进程。

**有界的探测。** `POST /v1/agent/model-connections/{id}/verify {"model": id}` 每次只验证一个模型：
占用一次每小时额度，三种 API 的原生检查同时进行，每种最多两次请求，所以一个模型最多六次服务商请求，
每种检查受原有超时约束。全部沿用固定解析地址的公网 IPv4 HTTPS 传输：不跟随重定向，不用代理，不回显
上游正文，key 不进日志、错误或参数。「保存并验证」和「验证未验证的模型」在前端每次最多依次验证三个
模型，并提示剩余数量；64 个模型的列表不会一次性扇出，服务端额度兜底。读取模型列表最多两次请求：先
OpenAI 风格，缺失或被拒时再 Anthropic 风格，所以只提供 Messages 的服务商在自动模式下也能列出模型。

**一并修复的独立评审问题。**

- F1（改名会轮换凭据）：仓库按实际值比较决定是否提升版本，接口忽略重新填入的同一个 key，表单只
  提交改动过的字段。
- F2（工具结果里的图片被换成 `[image]`）：图片不再被替换。工具消息保留文本并注明图片随后附上，
  图片以 `image_url` 内容放进这组工具结果之后的一条用户消息；并行的多个工具结果保持相邻且完整，
  Messages 的 `tool_result` 和 Responses 的 `function_call_output` 用同一映射
  （`protocol_compat/tool_images.py`）。支持图片的上游能看到图片，不支持的会返回明确的服务商错误。
- F3（「立即发送」忽略消息选的来源）：`begin_send_now` 在存储层比较消息与正在运行的回合；消息
  明确选了不同的模型或来源（Gateway 与个人连接之间、两个连接之间即使模型 ID 相同）时返回
  `model_mismatch`，消息留在队列里，接口返回 409。没有指定模型的消息继承当前回合，照常可以插入。
  界面上，选了别的模型时不提供「立即插入」，提示消息会作为下一轮运行。

**验证范围。** 后端：`tests/unit/test_model_connections_domain.py`、
`tests/servers/test_model_connections_api.py`、`tests/integration/storage/test_model_connection_store.py`、
`tests/integration/servers/test_pending_message_send_now.py`、`tests/unit/protocol_compat/`。前端：
`ModelConnectionsSettings.test.tsx`、`modelSource.test.ts`、`TaskComposer.modelSource.test.tsx`、
`PendingMessageQueue.test.tsx`、`JobDetail.enhancements.test.tsx`，以及浏览器用例
`e2e/model-connection-verification.spec.ts`（中英文、桌面和手机宽度）。全部使用模拟服务商。

## 十三、先验证再保存（2026-10-28 第二次修订）

用户的新要求：彻底去掉手动声明 API；表单要紧凑，不用大段多行输入框；先验证再保存，验证失败不能
留下连接或密钥。本节取代第十二节中与此冲突的部分，前文已按此改写。

**用户看到的。** 表单只有名称、Base URL、只写的 API key 和模型 ID。模型 ID 在一行里输入，回车或
「添加」把它变成可移除的小标签，长 ID 自动换行，全部可用键盘操作；最多 8 个。只有一个主按钮
「验证并保存」，后面是「取消」；不需要调用服务商的编辑（重命名、移除模型、没有改动）按钮显示为
「保存」。按钮下只有一句说明：验证会为每个模型向服务商发几个小请求，可能计费。验证失败时表单保持
打开，所有输入（包括密钥）都在，每个模型标签上写着自己的结果，改完可以重试；不会出现新卡片。卡片
只显示名称、Base URL、启用状态、每个模型及能用它的 Agent，协议和路径放在折叠的「技术详情」里，只读。
任何地方都没有协议单选框、复选框或开关。

**服务端：验证就在保存请求里。** 没有凭证（receipt），也没有草稿表。`POST` 和 `PATCH` 自己验证将要
提交的配置，再提交它，所以客户端无法跳过验证、伪造能力或重放旧结果：结果来自同一个请求，针对它要
提交的、已规范化的 Base URL、密钥和模型，属于已认证的所有者。

1. 校验并规范化输入。`PATCH` 读出所有者的当前行，算出真正的变化；密钥解密后比较，重新填入同一个
   key 不算变化。
2. 决定要验证哪些模型：新建或改了 Base URL / 密钥时验证全部要保存的模型；否则只验证新加的模型；
   重命名、移除模型、启用、停用、无变化时不验证，也不调用服务商。
3. 需要验证时：要求部署开关已打开；在 `agent_model_connection_checks` 里按模型数一次性预留所有者
   的每小时额度（每小时 20 次，按所有者的 advisory lock 原子计数，跨副本有效，连接不存在时也计入），
   要么全部预留，要么一个不占并返回 429，且不调用服务商。
4. 对每个模型调用 `verify_model`，最多同时四个模型；每个模型是三种原生检查、每种两次请求，沿用固定
   解析地址的公网 IPv4 HTTPS 传输，不跟随重定向、不读代理、不回显上游正文，密钥不进日志、错误或参数。
   调用期间不持有数据库事务。
5. 成功条件：每个要保存的模型都至少有一个 Agent 能用（从其原生结果推导）。否则返回 422
   `model_connection_verification_failed`，附上每个已验证模型的状态，什么都不写；失败的模型绝不会
   被悄悄去掉，也不会换成别的模型或服务商。
6. 提交：新建时一条 `INSERT` 写入行、密文和验证结果（版本 1），密文只在验证成功后才生成，所以失败的
   新建不会存下密钥；名称冲突在插入时返回 409。编辑时一条带条件的 `UPDATE`：只有当前行仍是未删除、
   `credential_revision`、`config_revision`、模型列表和已存验证结果都等于验证开始时读到的值才写入；期间发生的轮换、
   改地址、停用、删除或模型编辑都会让它落空，返回 409 `model_connection_changed`，原连接、密钥、结果和
   常驻进程都保持不变。期间的重命名不冲突，因为更新只写本请求改动的字段。新端点时两种版本号都加一，
   新结果按新的 `config_revision` 写入；否则保留其余模型的当前结果并合并新结果。
7. 不需要验证的编辑走原来的更新路径，并在 SQL 里限制为只能改名称、启用状态，以及把模型列表改成
   原列表的子集；Base URL、密钥或新增模型在存储层也无法绕过验证。

**限制与取舍。** 每个连接最多 8 个模型：换密钥或 Base URL 要重新验证全部模型，这个上限让一次保存最多
48 次服务商请求、8 次额度、分两批进行。总时限为 240 秒，超时返回 504
`model_connection_verification_timeout`，不写入连接或密钥；部署自带的边缘代理允许这个时长，额外代理需另行核实。手动声明、`protocols` 列、单独的 `/verify` 接口和逐个记录结果
的存储方法都已删除；请求里再带 `protocols` 会得到 422。模型列表发现只用于已保存的连接，选中的 ID
通过编辑添加，同样先验证。

**前端竞态。** 保存期间禁用「取消」，因为关闭表单不等于后端请求撤销。验证失败后保留全部输入，
恢复编辑和取消；修改任何字段都会清掉过期结果。编辑与停用、删除等操作重叠时不直接应用迟到响应，
等重叠操作全部结束后重新读取服务端列表；列表读取本身也按操作版本隔离，不能覆盖更晚的修改。
删除后返回的编辑不能让连接复活；离开页面后返回的结果不重新打开表单。移除后重新添加模型时，
其验证结果参与数据库条件更新，防止模型列表恢复原值后旧请求覆盖新结果。

**验证范围。** 后端：`tests/servers/test_model_connections_api.py`（新建失败不留连接和密钥、编辑失败
原样保留、部分模型失败、额度不足不调用服务商、重复名称和超过 8 个模型在验证前拒绝、手动声明被拒绝、
并发轮换/改地址/停用/改模型/删除时的 409、期间重命名不冲突、重命名不扰动、最多四个模型并发），
`tests/integration/storage/test_model_connection_store.py` 和
`tests/integration/storage/test_model_connection_verified_save.py`（真实 PostgreSQL 上的同类场景、
额度原子性、同一次读取的两次提交只能成功一个）。前端：`ModelConnectionsSettings.test.tsx`、
`ModelVerification.races.test.tsx`，浏览器用例 `e2e/model-connection-verification.spec.ts`（中英文，
1440px 浅色与 390px 深色）、`e2e/model-connections.spec.ts`、`e2e/dropdowns.spec.ts`。全部使用模拟
服务商。

