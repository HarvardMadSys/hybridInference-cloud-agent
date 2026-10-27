# 用户自带模型连接（BYOK）设计与实现记录

日期：2026-10-27
状态：已实现，等待合并；部署开关默认关闭，尚未上线，也未用真实服务商做端到端验收

## 一、要解决的问题

到目前为止，沙箱里的 Agent CLI 只能通过 Gateway 调模型：每个成员进程拿到一张由 Gateway
签发、按用户和模型限定的短期 grant，花的是 Gateway 给这个用户的额度。有些用户手里有自己的
OpenAI 兼容服务（OpenAI、OpenRouter、DeepSeek、公司内部对外开放的推理服务等），希望任务直接
用自己的 key 跑，账单记在自己头上。

本次做的是**用户自己的推理连接**，不是公开推理 API，也不是管理员出钱的共享 key：

- 连接只属于创建它的用户，不随团队共享，Fork 也不会把它带给别人；
- key 加密存储，对用户接口只写不读；执行所需的凭据只通过受认证的内部通道交给对应成员；
- 上游费用由 key 的主人承担，平台不从 Gateway 推理额度扣费，也不代设上游预算；
  已有运行时 token 统计仍可显示，但不将未知上游费用显示成零；
- 登录和「能不能用 Agent」（Agent admission）照旧走 Gateway。独立账号、排队名单、平台额度不在
  这次范围内。

## 二、范围

**做了的：**

1. 个人设置页「模型连接」：新增、编辑、轮换 key、停用/启用、删除、测试、从 `/models` 读取模型列表。
2. 每个连接：显示名、Base URL、API key、支持的协议（Chat Completions / Responses，可多选）、
   手填的模型 ID 列表（最多 64 个）。`/models` 只是辅助填写，没有它也能手填。
3. 新任务、后续消息、排队消息、目标（goal）自动轮次、原生 Fork 都能用连接上的模型，并且记住
   「是哪个来源的哪个模型」。
4. 测试按钮真的跑一次流式推理加一次工具调用往返，结果分成「支持 / 不支持 / 失败」三类。
5. 停用、删除、轮换 key 会让旧进程退出，新回合不会再用旧 key。

**没做的（有意不做）：**

- 通用协议转换器。不同 CLI 说不同的协议，连接不提供某个协议，就不能给那个 CLI 用。
- Claude Code 走连接。Claude Code 说 Anthropic Messages，连接不提供这个协议，一律拒绝。
- 连接模型的推理强度（reasoning effort）。没有可靠的模型能力表，只允许 Auto；带强度会被拒绝，
  从 Gateway 模型切过来时也会自动清掉。
- 在「我的设置」里把连接模型设为默认（pin）。pin 仍只针对 Gateway 模型；「沿用上次」会记住来源，
  见第五节。
- 连接任务的标题生成。标题生成用的是 Gateway grant，连接任务保留首行文字作为标题。
- 团队共享连接、管理员代付、私网地址例外。

## 三、协议矩阵

| 运行时 | 适配器说的协议 | 连接需要声明 | 说明 |
|---|---|---|---|
| OpenCode | Chat Completions | `chat_completions` | 由镜像里的包装脚本写入 provider 配置 |
| Kilo | Chat Completions | `chat_completions` | 同上 |
| pi | Chat Completions（`openai-completions`） | `chat_completions` | 同上 |
| Codex | Responses | `responses` | provider 的 `wire_api` 固定为 `responses` |
| Claude Code | Anthropic Messages | —— | 不支持，创建时拒绝；主机侧再拒绝一次 |

这张表放在共享词汇 `cloud_agent_shared/model_connections.py`（`RUNTIME_PROTOCOLS`），控制面和
主机都从这里读。判断「这个回合能不能在这个连接上跑」只有一个函数：
`domain/model_connections.require_usable`。它依次检查：连接存在且属于本人、未删除、已启用、
运行时有对应协议、连接声明了这个协议、模型在列表里、没有设推理强度。每种拒绝都有固定的原因码和
给用户看的句子，任何一种都不会退回 Gateway 或别的模型。

Base URL 原样使用，不像 Gateway 中继地址那样补 `/v1`：有的服务商路径是 `/api/v1`、
`/v1beta/openai`。CLI 自己在后面拼 `/chat/completions` 或 `/responses`。

## 四、数据和来源

迁移 `0106_user_model_connections`：

- `agent_model_connections`：`id`（`mconn_` + 32 位十六进制）、`user_id`、`name`、`base_url`、
  `protocols`、`models`、`api_key_ciphertext`、`credential_revision`、`enabled`、`last_check`、
  `deleted_at`。同一用户未删除的连接名字不能重复（不区分大小写）。
- `agent_model_connection_checks`：测试和读取模型列表的次数，用于每小时限额。
- `agent_model_connection_scan_keys`：仅保存确实绑定到成员常驻进程的密钥版本，以沙箱组、连接和
  凭据版本为范围加密保留，用于该组后续快照与受信发布的秘密扫描。新建但未使用的连接不会写入。
- `agent_jobs.model_connection_id`、`agent_pending_messages.model_connection_id`：NULL 表示
  Gateway。**不建外键**，删除连接不能把回合的来源改成 NULL，否则它就悄悄变成了 Gateway。
- `agent_thread_sandbox_memberships.residency_model_connection_id/_revision`：当前常驻进程
  是用哪个连接的哪个版本启动的。

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
- 连接只出现在其协议对应的 Agent 下（Chat Completions：OpenCode、Kilo、pi；Responses：Codex）。
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
5. **失效。** 轮换 key、改 Base URL 或协议、停用、删除都会提升 `credential_revision`（删除还会抹掉
   密文，只留墓碑）。提交后立即释放所有绑定在旧状态上的常驻，并通过主机关闭对应进程，走的是删除
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

## 八、服务端测试的网络防护

测试按钮会让控制面去拨用户填的地址，所以沿用 MCP 连接的防护：

- 只接受 `https`、443 端口、主机名或 IPv4（不接受 IPv6 字面量），URL 里不能带用户名密码、
  查询串或片段；
- 解析出的**每一个**地址都要通过 MCP 的公网策略（拒绝回环、私网、链路本地/元数据、CGNAT、组播、
  保留段、文档段）以及沙箱的受保护网段（含 `AGENT_EGRESS_PROTECTED_CIDRS`），且至少要有一个
  IPv4；
- 实际连接被钉在刚检查过的 IPv4 地址上，防 DNS 重绑定；不跟随重定向，不读环境里的代理设置；
- 上游返回的正文一律不回显，只保留 HTTP 状态码和从流里解析出的事实；读取有大小上限；
- 每个用户每小时最多 20 次（测试和读取模型列表合计），在数据库中原子预留次数以覆盖多副本并发；
  预留之后才发起网络请求，同一用户的多次测试可以同时进行，限制的是次数而不是并发。
  完成时校验被测配置版本，配置已变化则不覆盖当前测试记录。

测试流程：先发一个带一个工具的流式请求，要求模型调用它；再把工具结果带回去发第二个流式请求，
要求模型给出包含本次工具结果的最终回答。校验调用 ID、工具参数、预期结果和完整终止事件，
Responses 第二步保留第一步输出上下文。两步都成功才是该模型、该协议的测试通过，不能代表整个
连接上的所有模型均已兼容，也不等于真实 CLI 端到端验收。服务商能连上但做不到其中一步（404、400/422、
不是流、没调工具、没给最终回答）是「不支持」；连不上、证书错、重定向、401/403、429、5xx、
回答过大是「失败」。单纯 `GET /models` 成功不会显示为通过。

## 九、主机侧改动

- 认领载荷新增 `model_connection`，runner 解析后加入脱敏表。
- `ResidentLaunch` 和沙箱管理器的启动配置新增 `inference_base_url`、`inference_protocol`、
  `inference_source`，并参与「能否复用进程」的判断。沙箱管理器只替换 Gateway 和 MCP 中继地址，
  连接的 Base URL 原样保留。
- 运行时适配器用 `ExactBaseUrl` 类型区分「原样使用的地址」和「需要规范化的 Gateway 地址」，
  这样不用改动每个驱动的参数。连接只设置 `OPENAI_*`，不设置 `ANTHROPIC_*`；Codex 改 provider
  的 `base_url`；Claude Code 遇到连接直接拒绝。
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
  （不进参数、不设 Anthropic 变量），Claude Code 拒绝。
- `tests/servers/test_model_connections_api.py`：只存密文、不返回 key、422 不回显、跨用户不可见
  不可改不可测、轮换/停用/删除触发失效、测试限额；新任务不读 Gateway 目录、各种不适用组合在入队前
  拒绝；后续消息继承并复查来源、切换来源时的检查、排队消息遇到已删除连接被挂起；认领带 key 且
  不申请 Gateway grant、无能力主机退回、不适用时任务失败；常驻拿到绑定版本的 key、连接中途变化时
  释放并失败；`/config` 在 Gateway 目录不可用时仍提供连接和上次的连接默认值。
- `tests/integration/storage/test_model_connection_store.py`（真实 PostgreSQL）：所有者隔离、
  名字唯一、版本提升规则、墓碑不可复活、回合与后续消息的来源继承与切换、排队消息的来源、
  常驻绑定只认本人当前版本、新常驻代清空绑定、轮换/停用/删除后释放旧常驻且他人无法释放。
- 前端（vitest）：`ModelConnectionsSettings.test.tsx`（设置页增删改测与错误显示）、
  `modelSource.test.ts`（同名不同来源的身份、按协议过滤、旧草稿、不可用标注、来源切换的请求字段）、
  `TaskComposer.modelSource.test.tsx`（提交来源、无推理强度、草稿与 GitHub 回执保留来源、不可用时
  禁止运行且不退回 Gateway、改写禁用）、`JobDetail.modelSource.test.tsx`（继承、同名换源、最新回合
  与当前回合两条继承路径、继承来源不可用时禁止发送）、`SideChat.test.tsx`、
  `PendingMessageQueue.test.tsx`（只改正文保留来源）、`LedgerUsage.test.tsx`、`RunDetails.test.tsx`、
  管理员运行详情、`ModelSettings.test.tsx`。
- 浏览器（Playwright）：`e2e/model-connections.spec.ts`（1440px 与 390px 设置流程）和
  `e2e/model-source-attribution.spec.ts`（个人来源在刷新后保留、切换到同名 Gateway 模型、变为不可用）。
- 后端审查回归：`tests/servers/test_model_connection_usage_review.py`、
  `tests/unit/test_model_connection_message_review.py`，以及真实 PostgreSQL 的
  `tests/integration/storage/test_model_connection_attribution_review.py`（管理员查询与会话用量行
  保留来源，包括尚无尝试的回合）。

未覆盖：没有拿真实服务商做端到端验证；Kata 主机上的真实 CLI 走连接也没有在本分支跑过，
需要在 staging 用测试 key 跑一次 OpenCode 和 Codex。
