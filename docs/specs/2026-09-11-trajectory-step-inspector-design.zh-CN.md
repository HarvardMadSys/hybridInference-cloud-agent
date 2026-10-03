# Trajectory 步骤详情与 Run details 设计

- 日期：2026-09-11。
- 状态：交互方向已确认，并已按本文实现；实现与本文档在同一个 PR。
- 代码基线：`dev`，`b7a24c6`。实现前重新检查最新 `dev` 的相关变更。
- 目标仓库：`HarvardMadSys/hybridInference-cloud-agent`，本地目录名也可能为 `freeinference-cloud-agent`。
- 修订 2026-09-11：Tool 的 Payload / Result 不再内嵌已存事件记录（见 §6.2、§6.3）。原始记录由 Run 的 Raw 页签与 Session log 统一提供，step 不再各带一份。
- 修订 2026-09-11（二）：Tool 详情不再单独提供复制实参的按钮与 `Error flag` 行（见 §6.2、§6.3）。复制由 Payload / Result 块自带的 `Copy` 提供，失败状态由 Status 与退出码表达。

## 1. 已确认的产品决定

按照用户提供的 DS Harness 截图实现 Trajectory 右侧详情。Assistant 与 Tool 使用不同的信息结构，不能统一成同一套页签。

| 选中对象 | 详情页签，按顺序 | 标题区 |
| --- | --- | --- |
| Assistant | **Summary / Preview / Raw** | `ASSISTANT · Turn 7 · Step 14`，右侧关闭按钮 |
| Tool | **Summary / Payload / Result / Schema / Timing** | `TOOL · Turn 7 · Step 16`，右侧关闭按钮 |
| 某一轮 Turn | **Overview / Usage / Raw** | `RUN · Turn 7`，标题 `Run details` |

这些决定是实现约束：

1. 点击 Trajectory 列表行或时间线上的步骤，在右侧打开相应详情，列表保留在左侧。
2. Tool 的 Summary 同时预览 Payload、Result、Schema 和 Timing；每个分区标题可进入相应页签。
3. Tool 的 `Hierarchy → Assistant Message` 在关联有依据时跳到所属 Assistant。
4. Assistant 的 Summary 展示 Source、Status、Tokens、Reasoning、Content、Preview 和 Request Timing。字段是否有值由记录决定。
5. 恢复 **有明确轮次范围的 Run details**，入口是 Turn 标题或 Chat 对应轮次的 `View execution`。
6. `Completed` 可以出现在选中步骤/轮次的详情中；不恢复 Chat 顶部的 Completed 标签或 `Run completed` 成功横幅。
7. 工作区 **Git / Terminal / Files** 保留；工作区完整 Changes 不放入步骤详情或 Run details。
8. 页面只有一个右侧区域，步骤详情、Run details 和工作区在该区域内互斥展示。

原型中的数字、Schema 内容、父子关系和请求编号是示例，不能作为产品真实数据写入代码。本文给出生产实现的数据规则。

## 2. 要解决的问题

原来的 Run details 面向整次运行，内含的信息容易被理解为“这个任务的全部结果”。Chat 顶部的完成标签、完成横幅与对话内容重复；工作区 diff 又可能包含其他轮次及共享对话的修改。

在 Trajectory 中，用户先选择一个具体对象，再读取它的状态、输入、输出及时间，这些信息就有了明确范围。新的设计保留细节，但把查看细节的入口放在对应对象旁边。

完成后应满足三个问题：

- **这个 Assistant 输出了什么？** 查看 Assistant 的内容、来源、已记录用量及原始记录。
- **这个工具调用做了什么？** 查看实参、结果、工具定义及时间证据。
- **这一轮运行怎么样？** 查看对应 job/turn 的结果、执行信息及用量。

## 3. 当前代码事实与实现范围

以下路径均相对于本仓库根目录。

| 位置 | 当前行为 | 实现意义 |
| --- | --- | --- |
| `web/src/components/features/agents/TrajectoryView.tsx` | 事件列表、minimap、搜索、Session log；点击行后在行下方展开 `RowDetail` | 改为选中对象的右侧详情，不另建第二套事件读取 |
| `web/src/components/features/agents/trajectory.ts` | `toTrajectory` 一条 stored event 对应一行；按 attempt 与 call ID 配对工具结果 | 保留原始记录投影，增加显示步骤的索引与关联层 |
| 同上 | `tool_use` 当前 `actor` 为 `assistant`，lane 为 `tools` | 详情类型必须依据事件语义确定，不能直接拿旧 actor 决定页签 |
| `web/src/components/features/agents/JobDetail.tsx` | 管理 Chat / Trajectory 及右侧 workspace；Trajectory 位于 Chat 共用的 `max-w-4xl` 内容容器中 | 集中管理右侧区域；Trajectory 使用可用宽度，Chat 保留阅读宽度 |
| `web/src/components/features/agents/useAgentJobs.ts` | 已获取当前 job、thread.jobs 和全部 thread events；向页面返回经过折叠的 Chat job 与未折叠 events | 向 Trajectory 补充轮次/job 索引，不能从 Chat 的 `job.events` 反推原始轨迹 |
| `web/src/lib/api/agents.ts` | `AgentThreadEventApi` 包含 `job_id`、`turn_no`、event ID、attempt ID、seq、payload、时间 | 历史步骤和 Run details 可以定位到实际 job |
| `web/src/components/features/agents/LedgerUsage.tsx` | 现有用量读取、状态、刷新与降级处理 | 恢复详情时复用其数据语义，避免增加重复轮询 |
| `cloud_agent_host/runtimes.py`、`cloud_agent_host/drivers.py` | 各 runtime 已归一化 message/thinking/tool_use/tool_result，但保留的身份及完成信息不同 | 需要按 runtime 的已验证契约解读字段 |
| `cloud_agent_host/event_payloads.py` | 大于 256 KiB 的 payload 可能被截断，并带有标记 | Payload、Result、Raw 必须保留不完整提示 |

当前 `dev` 已通过 #145 移除旧 Run details 并保留 inline usage；#136 移除了其中重复的 diff。此次恢复的是 Trajectory 中的新详情，不回滚这些 PR，也不直接粘回旧 drawer。

### 3.1 本次必须实现

- 两套准确的 Assistant / Tool 页签、Summary 分区、关联跳转。
- 明确范围的 Turn / Run details。
- 正确的事件关联、工具状态、缺失与截断显示。
- 单一右侧区域的布局、关闭、键盘和窄屏交互。
- 保留原始日志与现有 Chat、工作区功能。

### 3.2 本次不扩展

- 不重写 runtime 协议、调度、计费、Fork 或 sandbox 生命周期。
- 不为还原截图而编造 token、TTFT、Schema、完成状态或父子关系。
- 不新增宿主机/容器资源仪表盘，不将工作区 diff 归因到单个 step。
- 不实现完整请求调试器、Replay 或从详情发起执行的功能。

UI 的全部页签是本次交付范围。新的 telemetry 采集属于后续能力；缺数据时页签仍存在，并明确显示未记录。

## 4. 页面结构与选择状态

```text
Conversation header                 Chat | Trajectory       Workspace
─────────────────────────────────────────────────────────────────────
Trajectory 列表区域                         │ 选中对象详情
  minimap / search                         │ TOOL · Turn 7 · Step 16  ×
  Turn 7 · 本轮摘要                         │ Summary Payload Result Schema Timing
    ASSISTANT  ...                          │ Hierarchy  Assistant Message ›
    TOOL       input → result       ←选中   │ Status     Completed
    ASSISTANT  ...                          │ Payload › ...
                                           │ Result › ...
                                           │ Schema › ...
                                           │ Timing › ...
```

### 4.1 唯一右侧区域

由 `JobDetail` 或其直接子组件持有选择状态，建议使用判别联合：

```ts
type RightPanel =
  | { kind: 'closed' }
  | { kind: 'step'; stepKey: string; tab: AssistantTab | ToolTab | EventTab }
  | { kind: 'run'; jobId: string; tab: 'overview' | 'usage' | 'raw' }
  | { kind: 'workspace'; tab: 'git' | 'terminal' | 'files' };

type AssistantTab = 'summary' | 'preview' | 'raw';
type ToolTab = 'summary' | 'payload' | 'result' | 'schema' | 'timing';
type EventTab = 'summary' | 'raw';
```

以上是内部 UI 类型示意，不是已有 API 字段。

| 操作 | 结果 |
| --- | --- |
| 初次打开 Trajectory | 不自动选中步骤，不强制打开详情 |
| 点击步骤或 minimap 标记 | 选中并定位步骤，详情默认 Summary；点击当前步骤仍保持打开 |
| 点击另一类型步骤 | 切换对应页签集合，回到 Summary |
| 点击 Turn 标题 | 打开该 job 的 Run details，默认 Overview |
| Chat 的 `View execution · Turn N` | 切到 Trajectory，定位 Turn N 并打开该轮 Run details |
| 点击 workspace 按钮 | 在同一右侧区域切换到工作区，详情关闭 |
| 点击 `×` 或按 Escape | 关闭当前右侧区域，将焦点还给打开它的元素 |
| 切到 Chat | 收起步骤/运行详情；显式打开的 workspace 可以保持 |
| 切换到另一个 conversation | 清空旧选择与其异步数据 |
| 同一 conversation 产生新 turn | 不抢走用户正在查看的历史步骤 |

关闭详情后，选中样式一并取消。可以在内存中保留最后选择，但不在重新进入页面时自动打开。

### 4.2 布局

- 默认详情宽度 400 px，允许拖动调整，范围 320–640 px；同时保证列表至少约 420 px。受实际容器宽度限制。
- workspace 与 inspector 可共用面板组件，但宽度偏好分别保存；复用 `useResizablePane`、`PaneResizer` 及 `PANE_RESIZE_EVENT`。
- Trajectory 不沿用 Chat 的居中窄阅读栏。minimap、搜索与列表使用右侧详情以外的全部区域。
- 桌面端列表和详情独立滚动；打开详情不重新定位整页，关闭后保留列表位置。
- 容器不足以容纳上述两栏时，详情接管主内容区并提供可见的返回/关闭入口；返回后恢复列表滚动位置。不能缩成不可读的两列。
- Tool 五个页签保持原顺序。窄屏允许页签栏水平滚动，当前页签始终可见。
- 视觉沿用本产品的浅色/深色主题；参考截图的信息层级、紧凑行距、文字区块与语法高亮。避免给每个分区套大型卡片。

### 4.3 键盘与无障碍

- 步骤、Turn、Hierarchy、分区跳转均使用可聚焦的真实按钮。
- 页签具备 `tablist`、`tab`、`tabpanel` 及相互对应的 `aria-controls` / `aria-labelledby`；支持左右键和 Home / End。
- 点击 minimap 或 Hierarchy 后，目标行必须进入视野。若搜索隐藏目标，清空查询后再定位。
- 搜索本身隐藏了当前选中步骤时，不改选其他行；详情注明该步骤不在当前搜索结果中，并提供清除搜索入口。
- 不在新事件到达时强制滚动或转移键盘焦点。
- Escape 只关闭本层面板；编辑框、terminal 或已有弹层正在消费 Escape 时尊重已有行为。

## 5. Assistant 详情

### 5.1 Summary

保持截图中的顺序与字段名：

1. **Source**：确有请求记录时显示可定位的请求标识；只有 native message identity 时显示该 message identity。没有记录则 `Not recorded`。`usage_id` 不能伪装成 Gateway request ID，内部 CLI RPC ID 也不能伪装成模型请求 ID。
2. **Status**：该消息的已记录状态，参见第 9 节。
3. **Tokens / Reasoning / Content**：只显示能归属到这个 native message/request 的用量，参见第 10 节。
4. **Preview ›**：展示消息正文、已记录 thinking 及其工具调用预览；点击进入 Preview。
5. **Request Timing**：Started、Total duration、TTFT、Generation、Throughput；只展示相应范围的时间证据。没有首 token 时间时使用截图中的 `First token unavailable`。

正文、thinking 和 tool-call block 按记录顺序展示。已记录的 thinking 默认折叠；没有 thinking 内容时不生成补充内容。

若请求来源不能进入真实的请求详情，Source 显示为文本；可以另提供进入该消息 Raw 的入口，不能做一个名为 Request、实际跳到无关页面的链接。Request Timing 的详细展开留在 Summary 内，不新增 Assistant 的 Timing 页签。

### 5.2 Preview

- 面向阅读的已存消息内容，Markdown 使用现有安全渲染方式。归一化器可能只保留 native response 的部分内容块，不能声称还原了未保存的全部响应。
- 已记录 thinking 按独立折叠区显示，不与最终回复拼成一个段落。
- 工具调用显示工具名和简短参数，点击进入对应 Tool 的 Summary。
- 有可靠 native message ID、但消息只携带工具调用时，保留 ASSISTANT 标签并列出工具调用，不增加占位文案。
- 不把 Raw JSON 当成 Preview，不用 `detail` 的截断单行替代完整内容。

### 5.3 Raw

- 显示组成这个 Assistant step 的原始存储事件，包括 ID、job/turn、attempt、seq、event_type、created_at 与 payload。
- 若该 step 由多条记录组成，每条记录单独标识；复制/下载可获取全部关联记录。
- 这是平台**存储的事件**，可能已被 runtime 归一化或截断；没有保存原始 provider 响应时，不能标成完整 provider response。

## 6. Tool 详情

Tool 调用使用 Tool 页签，即使底层事件由 Assistant 生成。配对结果不能落入 Assistant 的 Raw 页签代替 Result。

### 6.1 Summary

与截图保持同样的结构：

```text
Hierarchy     Assistant Message ›
Status        Completed

Payload ›
  { 实际调用参数 }

Result ›
  实际输出预览

Schema ›
  工具名
  当次工具定义的说明

Timing ›
  Started
  Duration
  Timing source
```

- `Hierarchy` 仅在有可靠 parent identity 时可点击；否则 `Not recorded`，不能使用“上一个 Assistant 行”猜关联。
- 点击四个分区标题分别打开 Payload / Result / Schema / Timing；从任意页签返回 Summary 均保留同一个调用。
- Summary 中的 Payload 和 Result 可限高为约 6–10 行；有明确的完整查看入口。展开完整页签时不再应用 Summary 截断。
- 成功状态用普通文本表达即可。错误状态适度强调，并与 Result 中的失败内容一致。

### 6.2 Payload

- 首选归一化事件实际保存的 `payload.input`。它可能为对象、数组、字符串或其他合法值。
- 对象/数组格式化并高亮；字符串按原文展示，不强制解析成 JSON。
- 空字符串、空对象、`false`、`0` 均是合法值，不用 `||` 误判成未记录。
- `detail` / `text` 只有在特定 runtime 的契约确认它就是实参时才可作为实参；否则标为预览或降级信息。
- 缺失显示 `Payload was not recorded.`。复制实参由 Payload 块自带的 `Copy` 提供，不另设按钮，不增加 Tool 的 Raw 页签，也不在 Payload 内重复调用事件本身。

### 6.3 Result

- 通过第 8 节的配对规则找到结果。支持 `content`、`output`、`aggregated_output`、`text` 等已验证的 runtime 结果字段。
- 显示可读输出与退出码，不内嵌原始结果记录；重复结果时只保留歧义提示，不列出记录本身。
- `is_error = true` 或有效非零退出码表示失败，不能只根据父 run 成功而显示 Completed。失败由 Status 与退出码表达，不再单独列出 `Error flag` 行；没有退出码时，失败只体现在 Status 上。
- 已收到空结果时显示 `Tool completed with no output.`；没有结果记录是另一个状态。
- 尚在等待结果时显示 `Waiting for result…`；已结束的 attempt 缺结果时显示 `Result was not recorded.`。
- 对运行中状态的判断必须基于匹配 attempt 的状态；不是基于当前页面最新的 job 是否还在运行。
- ANSI 内容用安全的文本/受限颜色渲染；不执行终端控制、HTML 或代码。

### 6.4 Schema

展示**调用发生时实际提供给模型的工具定义**：工具名、description、参数 JSON Schema；记录了版本或来源时一并展示。

当前事件契约没有保证每个工具都有完整 schema，V1 必须允许以下空状态：

> Tool schema was not recorded for this call.

不从实参反推 schema，不按工具名称硬编码一份通用 Bash 定义，不拿现在的 MCP schema 冒充历史调用时的定义。当前版本没有该字段时，空状态就是正确的产品行为，不能因此删除 Schema 页签。

### 6.5 Timing

默认字段顺序：Started、Duration、Timing source；有明确结束时间则补充 Finished。

证据优先级：

1. 对应 job + attempt + call 的 tool telemetry，且 `timing_source` 表明精确执行边界：显示 `Tool execution hooks`。
2. 正确配对的调用/结果事件时间：显示 `Session timestamps`。该区间是 call-record → result-record，可能包含排队、runtime/转发延迟；页签不为此另加说明。
3. 没有可用证据：`Not recorded`。

若精确 duration 来自执行钩子而只有事件接收时间，不能把事件接收时间标为精确 Started；分别标识 `Recorded at` 与 duration 的来源。不同证据的开始和结束时间不能混搭求差。

不将负数或无效时间修正成 0；显示不可用。`0 ms` 只有记录或合法计算确实得到零时才可展示。

## 7. Run details 的恢复范围

Run details 以选中的 **job / turn** 为范围，不随页面最新 job 改变。

### Overview

- Status、Turn、Started、Finished、Elapsed。
- Runtime、Model、显式配置的 reasoning effort。
- Attempt 信息；有重试时可切换 attempt 并注明结果与执行时间所属 attempt。
- 可恢复操作或失败说明复用现有 UI 规则；不要把成功运行的内部 publication note 恢复成醒目横幅。

### Usage

- 使用 `getAgentJobUsage(selectedJobId)` 及现有用量的来源/完整性语义。
- `ledger.totals` 是此 job 的统计范围，可能包含重试。`conversation.totals` 不能填到 Run Usage。
- attempt 用量放在所选 attempt 明细中，不用当前 attempt 的数据覆盖历史选择。
- 数据处于 pending / partial / stale / unavailable 时保留状态；没有成本数据时不显示 `$0`。
- 已有 Chat inline usage 保留。可复用同一按 jobId 缓存的读取，避免 Chat 和 Inspector 各自循环请求同一资源。

### Raw

- 按 `job_id` 筛选这一轮的 stored events；按 attempt 分组，允许复制与导出。
- 不重新请求整个 conversation 日志；优先使用 hook 已加载的 thread events。
- 区分 `Run log` 与全会话 `Session log`，避免导出的内容超出按钮描述。

### 不恢复的内容

- 不恢复旧 Run details 的完整 Changes 页签。
- 可以提供 `Open current Git diff` 链接；点击后切到 workspace 的 Git 页签。文字必须明确是当前工作区，不是“此轮变更”。
- 不恢复 Chat 顶部常驻完成状态、绿色完成横幅、Live workspace / Shared workspace 正常标签。

## 8. 从 Stored Event 到可查看的 Step

### 8.1 两层数据模型

现有轨迹以每条 stored event 为一行，新界面以可查看的消息/调用为 step。**增加 presentation projection，不改写或删减存储事件**。

```text
AgentThreadEventApi[] + thread.jobs
                ↓ 建立按身份划分的索引
原始事件表 ──→ Assistant / Tool / Event steps
    │                    │
    └─ Session/Run log   └─ Summary 与各详情页签
```

建议内部表示：

```ts
interface TrajectoryStep {
  key: string;                  // stable identity，不用可见序号
  jobId: string;
  turnNo: number;
  attemptId: number;
  stepNo: number;                // 当前 Turn 的显示序号
  kind: 'assistant' | 'tool' | 'input' | 'context' | 'system';
  anchorEventId: number;
  sourceEventIds: number[];
  parentAssistantKey?: string;
  resultEventIds?: number[];
  completion: 'running' | 'completed' | 'failed' | 'cancelled' | 'recorded' | 'unknown';
}
```

保留 `toTrajectory` / `toSessionLog` 原始语义，新增 `buildTrajectorySteps`（名称可调整）供 UI 使用。新数据层有自己的测试；不通过删除旧日志完整性测试来让新 UI 通过。

### 8.2 身份与顺序

- 使用 API 的 append order/event identity；不按可能重复的 wall-clock 时间重新排序。
- 关联至少限定 `jobId + attemptId`，再使用 native message ID / tool call ID。
- seq 在重试中可能重置；不能单独用 seq 作为跨 attempt 身份。
- Step 编号在当前 Turn 的完整投影上生成，搜索不重新编号。选择状态使用 stable key，不能用数组下标或 Step 号码。
- stable key 建议分别使用 `assistant:{jobId}:{attemptId}:{nativeMessageId}`、`tool:{jobId}:{attemptId}:{callId}`；缺少可靠 native identity 时使用包含 event ID 的独立记录 key。父消息与同源工具可共享 anchor，但 key 必须不同，同 anchor 时先呈现父消息再呈现工具。
- 新结果到达时填充已有调用，不新增第二个同义 Tool step，也不切换当前页签。
- 首次读取使用完整 backfill 后的投影；重连补齐历史数据可能校正显示编号，但仍按 stable identity 保留选中对象。

### 8.3 Tool call/result

- `tool_use.payload.id` 与 `tool_result.payload.tool_use_id` 匹配。
- 一对完整调用/结果显示为一个 Tool step；列表单行预览可为 `tool arguments → result`。
- Raw 日志仍包含调用与结果两条记录；Tool 的 Payload / Result 保留各自 stored event 的访问入口。
- result 不存在时调用仍显示；call 不存在时保留独立 `Tool result` step，Payload/Hierarchy 显示未记录，不能丢弃孤立结果。
- 孤立 result 后续补齐了 call 时，保留 event → canonical step 的别名映射，保证查看该结果的用户仍选中同一调用，不因 key 合并而跳到其他行。
- 同作用域出现重复 call ID、冲突结果时，不用 Map 的“最后一个值覆盖”悄悄决定。保留记录，标识关联不确定，停止计算依赖唯一配对的状态/耗时。
- 未知 runtime 的多结果/分块协议不得按单结果契约硬合并。只有已验证的适配器可以解释分块结束边界。

### 8.4 Assistant 与 Hierarchy

- 优先使用明确的 native message ID、parent message ID 等已记录关联。
- Claude 的部分归一化 payload 保留 `usage_id`，其来自 native `message.id`；在已验证同一 message 的范围内，可将正文、thinking 与 tool-call blocks 组织成一个 Assistant step。
- 同一 message 的 tools 仍各自有 Tool step，其 Hierarchy 指向该 Assistant；message 只有工具调用时仍呈现为 ASSISTANT 行，不增加占位文案，前提是 native message 身份确有记录。
- Codex / pi 等记录中若没有保留下来的 message 关联，就按单条 message/thinking 展示，Tool Hierarchy 显示 `Not recorded`。
- 不使用“最近一条 Assistant”、时间接近、文本相似或工具名作为父子关联依据。
- 不将两个不同 native messages 的重复文本去重为一个消息，不将整个 turn 合成一个 Assistant。

### 8.5 Other events

`input`、`context`、`lifecycle`、`error`、`raw`、`diff` 和未知 event type 仍可查看，使用适当 actor 与通用 **Summary / Raw**。不能为了只剩 Assistant/Tool 而把未知事件删掉。

已确认 identity 的 message-scoped usage 可关联到 Assistant；attempt-scoped usage 和无法归属的 usage 保留 context 记录，不硬挂到临近 Assistant。

### 8.6 Harness 报告事件

runtime 在启动时对自己的配置所做的陈述，作为可选证据字段 `harness` 挂在对应的 `lifecycle` 事件 payload 上。该 step **就是这份报告**，因此只提供读取它的两个页签：

| 选中对象 | 详情页签，按顺序 |
| --- | --- |
| 带 `harness` 报告的 lifecycle 事件 | **System Prompt / Tools** |
| 其余事件 | **Summary / Raw**，不出现空页签 |

对带报告的事件不再提供 Summary 与 Raw：这是与 DS Harness 入口对齐的结果（DSH 的 "Initial System Prompt" 同样只有这两个页签）。代价是这条事件的原始记录不再从该行进入 —— 若需要，应另加入口而不是把页签加回来。选中该类 step 时的默认页签是其第一个页签，不是固定名称，否则页签条与面板会对"当前打开哪个"说法不一致。

数据边界：

1. **没有 system prompt 可展示。** Claude Code 在 CLI 内部组装 system prompt，从不写入 stream-json；这一点对着镜像锁定的 2.1.220 二进制核对过。`System Prompt` 页签因此明确说明平台从未收到该文本，并展示 runtime 确实陈述过的会话配置（runtime 版本、model、permission mode），不编造提示词。
2. **`Tools` 页签展示 runtime 自己列出的工具名**，以及它报告的 MCP server 与状态；CLI 不上报每个工具的参数或 schema，因此不展示这两项。
3. **只取会话自身的 init。** 子 agent 会发出同一 subtype 但范围更窄的 init，归一化事件没有区分标记，因此不把它的清单当作会话的清单。
4. **名称照抄，不从命名约定推断归属**；工具名上限 200 条，越界通过 `tools_truncated` 显式报告，不静默截短。
5. **旧数据兼容。** 改动前存储的 lifecycle 事件没有 `harness` 字段，这些 step 保持 Summary / Raw，不显示空页签。payload 被平台限额截断成文本时同样不显示，因为文本不是"加载了什么"的证据。

实现位置：`cloud_agent_host/runtimes.py`（采集与归一化）、`trajectorySteps.ts`（投影与拒绝规则）、`HarnessDetails.tsx` 与 `TrajectoryInspector.tsx`（页签）。

## 9. 状态规则

| 可确认的证据 | 展示 |
| --- | --- |
| 已记录的工具结果明确错误或有效非零退出码 | `Failed` |
| 工具结果明确完成，且没有错误证据 | `Completed`，即使输出为空 |
| 调用已开始，匹配 attempt 当前仍 active，尚无结果 | `Waiting for result`；仅在有明确执行中证据时用 `Running` |
| attempt 已结束，但调用缺结果 | `Result not recorded`，不推断成功或失败 |
| 该 step 本身有明确取消事件 | `Cancelled` |
| 已验证 runtime 发出的完成消息/内容块 | 可显示 `Completed`，其范围只是该消息/内容块 |
| 仅有已存文本，无法判断消息完成边界 | `Recorded` |
| 没有可靠状态 | `Unknown` 或 `Not recorded` |

父 run 的 `succeeded` 不能证明所有工具成功；父 run 的 `failed` 也不能把之前完成的工具改成 Failed。一个 message 的 Completed 不表示整个用户任务完成。

## 10. 用量、时间与 Schema 的数据证据

2026-10-04 更新（#229）：Assistant 请求统计通过当前用户拥有的 job、指定 attempt
的 ledger 引用和精确 native response ID 读取网关记录。网关独立保留响应 ID 元数据，
不依赖完整 prompt/response 日志。要求网关回显 ID 筛选条件，并校验 grant、时间窗口、
thread 和返回行的 ID；存在多个匹配时不归属统计，不按顺序或邻接时间猜测。

网关的请求开始时间、TTFT、总耗时和 output/input tokens 可用于此消息的详情。
Generation = latency − TTFT（仅在两者已知且非负时）；Throughput = output tokens / Generation
（仅在 Generation > 0 时）。未采集的细分不展示为空行，历史记录与旧网关显示未关联原因。
事件入库时间差不再显示为模型请求耗时。日志异步写入时做有限次数重试，并提供手动重试。

| 详情字段 | 当前可以使用的证据 | 禁止替代方式 |
| --- | --- | --- |
| Tool Payload / Result | 匹配事件的实际字段 | 单行预览冒充完整实参/结果 |
| Tool Hierarchy | 明确 message identity / parent identity；Claude 经验证的 usage_id 关联 | 最近 Assistant、相邻 seq、同工具名 |
| Assistant Source | 实际保存的 native message/request 标识 | 自造 `Request #N`；将 CLI 控制协议 request ID 当成模型 request ID |
| Assistant tokens | 精确 response ID 关联的网关用量，或同 native message 的 usage 及明确的 usage scope | 整轮/全会话 totals 平均分配到 step |
| Assistant Request Timing | 精确 response ID 关联的网关请求 started/TTFT/latency；或同请求记录的 started/first-token/finished | 相邻 Assistant 的 created_at 差；thread-level TTFT |
| Tool Timing | 匹配调用的 telemetry 或事件时间区间 | 混合来源；跨 attempt 配对；负值夹为零 |
| Tool Schema | 当次调用有版本/来源依据的定义快照 | 根据实参猜 schema；当前全局工具定义冒充历史快照 |

### 10.1 Token 语义

- Assistant 的 `Output tokens` 指该 native message/request 的生成 token；`Reasoning tokens` 与 `Content tokens` 仅在数据源明确提供时展示，不包含输入 token。网关用量优先于 runtime 的临时计数，并注明数据来源或估算标识。
- 已关联的网关输入用量单列 `Input tokens`，不将 input + output 标为输出 token。
- 同一 `usage_id` 在 text/thinking/tool blocks 上可能重复；按 identity 去重，不能求和重复 usage。
- `usage_scope = attempt` 明确属于 attempt，不填到某个 Assistant。
- 若一个 runtime 的 output 是否包含 reasoning 不明确，Content 显示未记录，不能直接做减法。只有语义确认且非负时才允许 `content = output − reasoning`。
- `null` 与缺失表示未记录；0 仅表示实际记录为零。数值使用 finite/nonnegative 检查。
- 消息级用量不要再累加成一套“新账单”；Run Usage 继续使用现有 ledger。

### 10.2 首 token 和速率

没有已记录请求开始与 first-token 时间，不展示 TTFT 数值；没有 first-token 与结束时间，不计算 Generation 与 Throughput。不能把最后一条完整 message 到达的时间当成 first token。

### 10.3 截断

- 尊重 `truncated`、`original_payload_bytes`、`truncation_notice` 及已有文本截断提示。
- 完整页签表示“完整的已存内容”，不声称恢复被 runner 截掉的原始内容。
- 完整日志导出同样保留截断标记。

## 11. 数据读取与组件实现建议

### 11.1 现有 API 足够完成首版布局

- `listAllAgentThreadEvents(jobId)`：整条 conversation 事件。
- `getAgentJobThread(jobId)`：thread.jobs 与消息信息。
- `getAgentJob(selectedJobId)`：必要时按需刷新选中 Turn。
- `getAgentJobUsage(selectedJobId)`：所选 job 的用量。
- `getAgentAttemptTelemetry(selectedJobId, attemptId, signal?)`：按需获取对应 attempt 的工具时间信息；第三个参数支持 AbortSignal。

`useAgentJob` 已读取的 thread.jobs 应通过显式 DTO/索引传出，不向子组件重复请求整条 thread，也不把“当前 job”借给所有历史 Turn。

所有加载按 jobId/attemptId 定位，使用 AbortController 或 generation guard 防止快速切换时旧响应覆盖新选择。关闭隐藏面板后停止其专用轮询；共享缓存仍可由现有 Chat 读取使用。数据请求失败只影响详情，不清空左侧事件列表。

### 11.2 建议文件划分

| 文件 | 责任 |
| --- | --- |
| `trajectory.ts` 或新增 `trajectorySteps.ts` | 纯数据投影、关联、稳定 key、状态与证据选择 |
| `TrajectoryView.tsx` | minimap、搜索、Turn/Step 列表、选择事件 |
| 新增 `TrajectoryInspector.tsx` | 根据 kind 分发页签、关闭和共同 header |
| 新增 `AssistantStepDetails.tsx` | Assistant 三页签 |
| 新增 `ToolStepDetails.tsx` | Tool 五页签 |
| 新增 `RunDetails.tsx` | 轮次范围 Overview / Usage / Raw |
| `JobDetail.tsx` | 唯一右侧区域、Chat/Trajectory/workspace 切换 |
| `useAgentJobs.ts`、必要的 display 类型 | 传出已有轮次信息，支持历史选择 |

文件名可随现有约定调整，但不把身份推断、用量计算和所有页签堆进 `JobDetail.tsx`。共用 JSON/文本查看器时优先复用已有依赖，不为语法高亮引入新编辑器框架。

### 11.3 后续采集扩展

若要让尚未记录的 Source / Schema / 请求时间在新运行中完整出现，需要另行扩展 runtime→stored event 的可选证据字段，例如 native message identity、parent identity、schema snapshot、模型请求时间与 usage scope。

这些不是当前 API 已存在的标准字段。不能只在 TypeScript interface 里声明后就假设后端会返回。采集扩展需逐 runtime 验证，保留 payload 限额、归属边界和旧数据兼容；本次 UI 可先以明确空状态上线。

已落地的一项：`harness`（见 §8.6）。Claude Code 适配器此前只保留 init 事件的 `phase`、`model`、`runtime_version`、`session_id`，把工具清单丢掉；现在同时保留工具名、MCP server 与 permission mode，并按上述规则归一化。其余 runtime 目前不报告这些字段，因此它们的 step 不出现对应页签 —— 这是"未报告"，不是"没有工具"。system prompt 本身无法采集：CLI 不上报它，除非将来由平台自己注入（`--append-system-prompt` 之类）才有文本可存。

## 12. 原始内容与权限

- 只通过现有 owner-facing API 读取当前用户可查看的 job/thread。
- Raw、Payload、Result、Schema 均为数据展示，不执行内嵌命令、脚本、HTML 或 Schema 描述中的指令。
- Markdown 不开启 raw HTML；JSON/文本使用转义渲染，不把 payload 注入 `dangerouslySetInnerHTML`。
- Tool Schema/请求详情不得为了补齐界面读取其他用户配置、全局密钥或宿主机环境。
- 不通过“重新运行工具”生成缺失结果。

## 13. 验收标准

### 13.1 交互验收

1. 初次打开 Trajectory 详情关闭；点 Assistant 后准确出现 Summary / Preview / Raw。
2. 点 Tool 后准确出现 Summary / Payload / Result / Schema / Timing，没有 Preview 或额外 Raw 页签。
3. Tool Summary 含 Hierarchy、Status 及四个预览分区；每个分区正确跳转。
4. 有明确父消息时 Hierarchy 可来回定位；无证据时显示未记录，不跳到相邻消息。
5. 点击 Turn 2 后 Run details 展示 Turn 2 的 job 数据，即使当前聊天最新一轮为 Turn 7。
6. 打开 workspace 会替换详情，页面不同时显示两块右侧面板；Chat 和 terminal 原有使用行为不回归。
7. Chat 不重新出现 Completed/Run completed 横幅；inline usage 保留。
8. Git diff 只在工作区完整查看；Run details 中的入口标明 current workspace。
9. minimap 选择、搜索、关闭、Escape、键盘页签、拖动调整宽度可用；窄屏可进入和返回详情。

### 13.2 数据验收

必须覆盖以下有实际风险的场景：

- 同一 call ID 在不同 job / attempt 复用；不得串结果。
- 同一 native message 的重复 usage 不重复计数。
- 没有 parent identity 的 Tool 不做邻近归属。
- 孤立 result、没有 result、空 result、失败 result、重复或冲突 ID。
- 原始 payload 为 null、字符串、对象、空对象、0、false；不得丢合法值。
- 无 Schema / 无 TTFT / 无 reasoning tokens 的完整页签及缺省展示。
- 实际 `0 ms` 与无效/负时间区分。
- 大输出截断标记在 Summary、完整页签和日志中保持可见。
- SSE 到达结果后更新已选调用，保留页签、焦点与滚动。
- 快速切换历史 Turn、切到另一 conversation 后旧网络响应不得覆盖新面板。
- 多 runtime、raw-only 运行、未知 event type 仍可查看。

### 13.3 验证命令与页面检查

前端执行现有门禁：

```bash
cd web
npm run lint
npm run format:check
npm run type-check
npm run test
npm run build
```

纯投影规则在 `trajectory.test.ts` 或新增数据测试中验证，页签/选择/历史范围在 `TrajectoryView.test.tsx` 和相关组件测试中验证。

保留 `toSessionLog` 的原始事件完整性测试。新增 UI 使用 step 数量时，不能继续把它叫 event count；可显示 `N steps · M events`。工具结果被纳入对应 Tool 后，证明它仍能在 Result、Run 的 Raw 页签和 Session log 中读取。

浏览器人工验收浅色/深色、宽屏/窄屏、一个 Assistant、一个成功工具、一个失败或缺结果工具、一个历史 Turn。模拟数据检查与实际部署检查分别记录，不能将原型截图当成真实 API 验证。

如果实现同时扩展后端采集，再运行对应 runtime/store/API 的测试和后端门禁；UI-only PR 不顺便改后端存储架构。

## 14. 推荐实施顺序

1. **纯数据层**：明确 stable identity、call/result 配对、native message 关联及缺失状态；先解决跨 attempt 和重复计数风险。
2. **面板与布局**：单一右侧区域，接入选择与关闭；保持 Chat / workspace 行为。
3. **Assistant / Tool 详情**：按已确认的页签和 Summary 顺序实现，接真实记录；无字段时使用规定空状态。
4. **Run details**：按实际 job/attempt 加载历史范围信息；接现有 usage 数据流。
5. **异步与无障碍**：SSE 更新、取消请求、焦点/滚动、搜索定位、窄屏行为。
6. **验证与 PR**：相关测试、完整前端门禁和浏览器验收；描述 UI 已完成与数据尚未采集的区别。

## 15. 给实现者的交付说明

本设计用于交给 DS 4.1 实现。用户已经确认按 DS Harness 的 Assistant 三页签、Tool 五页签方向落地；不用再次讨论是否统一页签，也不要将此工作改成只有静态 mock 的交付。

实现时从最新 `dev` 开始，保留本文标注的当前数据边界；先检查是否已有其他 PR 改动上述组件。实际落地若需要修改身份采集/用量归属契约，清楚拆出它的范围，不把未经验证的数据猜测写进前端。

最终交付包括可运行的交互、测试与构建结果、真实数据缺项说明及 PR。实现见同一 PR：新增 trajectorySteps.ts 投影、TrajectoryInspector / AssistantStepDetails / ToolStepDetails / RunDetails 组件，以及 JobDetail 的单一右侧区域；未改动后端采集契约。


## 16. 2026-09-17：消息轨迹与运行日志分离

PR #264 参考 DSH 的消息归属逻辑，将主列表的阅读单位调整为 User / Assistant / Tool：

- 每轮输入来自 job 的 `task_prompt`；原生 userMessage 回显作为同一输入的来源，不另计步骤。没有 job 输入时使用原生记录。重试不重复用户输入。
- Codex adapter 为完成的消息保留 `native_item_id`，为原始通知保留结构化内容。开始、流式片段和完成记录按 job / attempt / item 更新同一条 Assistant；完成快照替换片段，不重复显示或改变 Chat 的最终回复。
- ASSISTANT 表示一条模型消息，可以包含思考、正文和多个工具请求；只有工具请求的消息仍属于 ASSISTANT，无正文时不填占位文案。按已记录的消息 ID 归并，同一消息发出的多个请求共用一个父行，工具调用与结果按 call ID 配对。每个请求都保留在父消息的 Raw 来源中。
- Codex 的工具 item ID 只标识工具，当前采集未记录它所属的 provider message ID。因此保留独立 TOOL，不按每个工具补造 ASSISTANT，也不按相邻位置、token 用量通知或同一用户轮次猜测消息归属。已有真实 `usage_id` 的记录可以建立消息与工具的父子关系。
- Tool 在列表显示名称、参数 → 结果；未完成输出可预览，但有输出不等于成功。详情继续保留调用与结果的配对证据。
- 正常 lifecycle 和已识别的协议通知进入 Run logs，保留 Raw 和完整 Session log 下载，排除在主列表、步骤编号和 overview 之外。流式片段保留在对应消息/工具的 Raw 来源中。
- CONTEXT 只用于明确记录的上下文输入，SYSTEM 只用于明确记录的系统提示内容。token 用量不是上下文消息：有真实消息归属的用量附在对应 ASSISTANT，取最新快照，不累加同一消息重复出现的计数；轮次总量、未匹配用量进入 Run logs，不增加模型消息数。当前 adapter 没有采集到的上下文或系统提示不补造。
- 错误、警告和未知记录仍直接可见；运行时记录标记为 Runtime，不将后台通知称为 System prompt。harness 初始化配置进入 Run logs，保留原来的工具清单与 MCP 详情入口。
- 空的已结束 Assistant 进入 Run logs；仍在生成的 Assistant 保留位置。每轮有 Turn 边界，overview 与搜索栏随列表滚动保持在顶部。

参考源码：[Assistant 消息组装](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-trajectory/src/client/trajectory-assistant-definition.ts)、[User / Context 来源分类](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-trajectory/src/client/trajectory-message-definitions.ts)、[工具调用与结果配对](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/client/ui-trajectory/src/client/trajectory-tool-definition.ts)。DSH 以真实 turn / step 和消息内容块组装 ASSISTANT，以输入来源区分 USER 与 CONTEXT；角色颜色不决定这些语义。

兼容性：旧记录缺少完成消息的 native item ID 时，不能可靠还原流式片段与最终回复的对应关系，因此旧片段仍在 Run logs，最终消息照常显示。新 host 采集生效后才有完整的流式关联。Codex 工具所属模型消息仍是采集缺项；本次没有将 item ID 冒充 message ID。此改动对齐已记录消息的处理规则，不声明与 DSH 的完整消息采集、Duration / Turns / Calls、provider 调用计量及虚拟列表完全一致。
