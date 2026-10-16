# Admin Usage 聚合页设计

状态：供 DS 实施的设计提案；本文不代表功能已经实现。

代码核查基线：Agent `dev`，`7ce1cc8`。本期延续已合并的 [Admin 观测面 #150](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/150)。

## 1. 要解决的问题与交付范围

管理员已经能在 Admin → Runs 逐个查看任务。下一步需要从同一批数据回答：所选时间创建的任务累计记录了多少推理用量，分别属于哪些用户、哪些任务配置模型，数据缺了多少。

本期交付 **Admin → Usage**，包含一个只读聚合接口、一个页面，以及从分组跳到对应 Runs 列表的链接。只使用 Agent 已保存的 Gateway 账本快照；保留与现有单任务页面相同的计数和缺失值语义。

本期不包括服务器容量、整轮资源采集、逐请求 LLM 明细、告警、导出、执行排障、失败分类或重试分析。上述工作仍在可观测性后续范围内。生产部署不在本期验收范围。此前 review 遗留的事件内容判定和其他页面格式清理不混入这个 PR。

无需新增服务、依赖、数据库表、账本刷新任务或网关契约。页面不读取 Gateway 数据库，不新增 Gateway 用量查询；现有管理员鉴权仍可能向 Gateway 核查当前权限。

## 2. 已定的产品与统计决策

| 项目     | 本期决定                                                              |
| -------- | --------------------------------------------------------------------- |
| 用户     | 具有 `agent.admin` 权限的管理员                                       |
| 统计对象 | 一个 run = 一个 `agent_jobs` 任务/对话轮次，包含它的全部历史 attempts |
| 时间选择 | 按任务创建时间选择 `[since, until)`，默认最近 7 天，最长 90 天        |
| 日分组   | 按任务创建日，UTC；同一任务全部已缓存用量归入同一天                   |
| 模型分组 | 任务记录的配置模型；不是实际服务模型、路由或供应商                    |
| 数据来源 | 已验证的 Gateway 快照；不把 runtime 自报数补进缺失快照                |
| 金额     | 管理员可见，称“已记录费用”，不是已结算账单                            |
| 默认排名 | 已记录费用降序，用户和模型各前 20 组，剩余组合并为“其他”              |
| 更新     | 打开、应用筛选或点 Refresh 时重读缓存；不新增自动轮询或账本刷新       |
| 内容     | 不获取 prompt、标题、事件内容、错误原文，也不提供内容揭示动作         |

### 2.1 时间口径必须显示在页面上

例如任务在 9 月 10 日 23:59 UTC 创建、9 月 11 日结束，它的整个已缓存用量都归到 9 月 10 日。筛选结束时间在任务执行途中，也不会把该任务的快照按比例裁掉。不同日期区间选中互不重叠的任务，但以后查询同一区间时，迟到快照可能使总量变化。

页面固定说明：

> Usage recorded for runs created in this period. Includes their full cached usage. Dates use UTC.

中文含义是“所选时间创建的任务的已记录用量，包含这些任务完整的缓存用量，日期按 UTC”。不要命名为“当天实际消费”或“本月账单”。现有 totals-only 快照无法恢复请求发生日；以后接入请求明细才能新增那种统计口径。

### 2.2 模型与用户口径

- 分组和筛选使用任务自身的 `model` 和 `user_id`。模型列标题为 **Configured model**。
- 不从当前 thread 配置或最新 residency 反填历史任务；若实施时字段被重构，使用保存该次执行配置的等价字段。
- 用户显示完整 ID 的安全文本，可用 CSS 省略并提供完整值；不逐行调用 Gateway 获取邮箱或姓名。
- 分组使用完整原始键，不能先 `LEFT()` 截断再分组，避免不同用户或模型被合并。页面用文本渲染，链接参数用 `URLSearchParams` 编码。
- 空字符串或 NULL 的历史模型组成一个明确的 `Unknown configuration` 组，不猜测模型。详情见 §5 的缺失模型处理。

## 3. 数据来源与求和规则

以 [账本快照设计](2026-09-09-inference-ledger-snapshots.md) 和当前 `cloud_agent/ledger.py`、`cloud_agent/grant_usage.py` 为准。

```mermaid
flowchart LR
    A[创建时间与精确筛选] --> B[Agent 任务集合]
    B --> C[全部 attempts 与账本引用]
    C --> D[复用单任务账本校验和汇总]
    D --> E[总计与 UTC 日分组]
    D --> F[用户与配置模型分组]
    F --> G[前 20 组与其他组]
```

### 3.1 每条快照只计算一次

1. 每个任务取全部 attempts，通过 LEFT JOIN 保留无账本引用的历史 attempt。未开始、没有 attempt 的任务也必须保留在任务计数中。
2. 每个 `agent_attempt_ledger` 行只进入一次求和。同一 attempt 换 grant 时可以有多行，全部计入。
3. 不按 `grant_id` 全局去重：常驻会话的不同轮次可能使用同一个 grant，但保存的是不同窗口。也不把同一 grant 的整段生命周期用量重复算到每轮。
4. 已成功、失败、取消的 attempts 都计入；不因任务失败而减去已发生的用量，不重新查询或重建窗口。
5. Fork 的任务各自计入，即使共享 sandbox；不以 thread、workspace 或 sandbox 再去重。Fork 时为显示而复制到子对话的源轮次（`fork_source_job_id` 不为空）没有运行过，不是任务，不进入任务集合。
6. 只读每个引用当前保存的 snapshot。刷新次数和刷新阶段不是账本版本列表，不能累加同一引用的多次读取。

### 3.2 复用现有校验与状态

按 job 分组后复用 `summarize_ledger()`，包括 `GrantUsageReport` 校验、grant 匹配、部分缺失、刷新失败保留旧值、`CORE_METRICS` 和 `estimated_calls` 规则。

没有 attempt 的任务：非终态为 `pending`，终态为 `unavailable`，与 `summarize_job_usage()` 当前特殊处理一致。若抽取一个只处理 job ledger 的公共函数，让 owner、Admin Run 和本接口都调用它；不向 `summarize_job_usage()` 塞伪造 runtime 输入。

聚合不调用 `usage_inputs()`，不读取 `agent_job_events`，不计算 runtime 用量，也不为了每个任务发一次 SQL/API 请求。

### 3.3 数值与缺失值

| 情况                         | 返回与显示                                                               |
| ---------------------------- | ------------------------------------------------------------------------ |
| 没有任何有效快照             | `totals=null`、`unknown_rows=null`，显示 Unavailable                     |
| 有效快照 `calls=0`           | 保留真实的 0；token/费用按现有契约仍可为 NULL                            |
| 某指标一部分已知、一部分未知 | 只求已知部分之和，同时累加该指标的 `unknown_rows`                        |
| 某指标全部未知               | 保持 NULL，不能 `COALESCE(..., 0)`                                       |
| 缺一整个 attempt 的快照      | 增加 `missing_attempts`；无法知道缺失了几次请求，不能编造 `unknown_rows` |
| 刷新失败但有上次有效快照     | 旧值仍贡献总计，保留现有 stale/partial 状态和查询时间                    |
| 只缺 reasoning               | 保留缺失计数，不因此把完整的核心用量降成 partial                         |
| 某请求用量是估算值           | 原样累计 `estimated_calls`，说明“有 X 次请求包含估算用量”                |

`tokens_in` 沿用网关定义，包含其已计入的缓存输入；不再额外加 `cache_read` / `cache_write`。它们作为细分指标保留。`reasoning` 也不另加到 `tokens_out`。

金额在后端用 `Decimal` 求和和排序，以十进制字符串返回。共享或小幅提取 `_sum_reports()` 的已知值求和逻辑，避免新增一套 SQL JSON 强制转换规则。不要制造假的 `GrantUsageReport` 来表示一个聚合组。

这里有一个实施时须修正的现有边界：单份合法金额可包含 128 位整数，另一份可包含 126 位小数，相加结果可长达 255 字符；当前 `_sum_reports()` 的 precision=160 和 `GrantUsageTotals` 的 128 字符上限不足以表达它。同一 job 内就可能先失败，不能仅拓宽最终跨任务 response。

原始 `GrantUsageReport` 金额校验仍保持 128 字符；**所有派生 job/conversation/admin summaries** 使用独立的 totals 读模型。共享 reducer 根据操作数的整数位、小数位与项数设置足够的 Decimal precision，返回**完整精确和**：任何量级、任何小数位都不做舍入，也不截断。显示精度只属于页面，不得进入 reducer——否则 `0.01 + 0.00012345` 会变成 `0.01`，同一批请求因报告如何分到 jobs 而得到不同总量。派生金额宽度按 50,000 行范围内的最坏情形取值（128 位整数 + 5 位进位 + 小数点 + 128 位小数 = 262 字符），并留出余量。普通既有输入的字段、值和状态保持不变；为同 job 和跨 job 的大整数+小数、小额叠加都加回归测试。这是共享求和的边界修复，不是放宽网关原始报告契约。

### 3.4 覆盖率、新鲜度和“已结算”是三件事

- `runs_with_snapshot / run_count` 只表示“有至少一份有效快照的任务比例”。一份零调用快照也算有快照；部分 attempts 缺失的任务也可能在分子内。页面不能把这个比例写成“账本完整率”。
- `missing_attempts`、各状态任务数以及 `unknown_rows` 同时返回，才能解释剩余缺口。覆盖率分母为 0 时显示无任务，不显示 100%。
- `oldest_queried_at` 是参与求和的快照中**已知**查询时间的最小值，不是“所有数据完整到此刻”。没有时间戳时返回 NULL。
- `generated_at` 是本次读取的数据库时间，不是所有账本重新查询的时间。
- 当前运行中任务不保证有自动刷新的快照。Admin Refresh 只读现存值；页面写“Cached usage; active runs may not be counted yet”。
- `finalized` 固定为 `false`。任务结束五分钟、刷新到第三阶段或没有待刷新任务，都不能被写成“已结算”。

## 4. API 契约

新增独立路由：

```text
GET /admin/agent/usage
    ?since=2026-09-04T00:00:00Z
    &until=2026-09-11T00:00:00Z
    &user_id=...
    &model=...
    &runtime=...
    &repo=...
```

| 参数             | 规则                                                                                                |
| ---------------- | --------------------------------------------------------------------------------------------------- |
| `since`, `until` | 有时区的 ISO 8601；只有一端时按现有 Runs 默认规则补全；转换 UTC；`since < until` 且跨度 ≤90×24 小时 |
| 都省略时间       | 以一次捕获的当前时间作为 `until`，`since=until−7 天`                                                |
| `user_id`        | 可选、精确相等、最长 256                                                                            |
| `model`          | 可选、精确相等、最长 256                                                                            |
| `runtime`        | 可选、精确相等、最长 128                                                                            |
| `repo`           | 可选、精确相等、最长 256                                                                            |

前端空输入省略，后端拒绝显式空字符串。非法时区、超长参数、倒置/过长窗口、未知参数在执行聚合 SQL 前返回 422。本期没有 host、状态、全文搜索、请求时间、served model 或任意 `group_by` 参数；收到这些筛选也应拒绝，不能忽略后返回更大的任务集合。

### 4.1 响应结构

以下是字段契约，不是 JSON 实例。三个分组和 summary 必须来自同一次数据库读取视图。

```typescript
type Totals = {
  calls: number;
  tokens_in: number | null;
  tokens_out: number | null;
  cache_read: number | null;
  cache_write: number | null;
  reasoning: number | null;
  spent_usd: string | null;
};

type UnknownRows = Record<
  | "tokens_in"
  | "tokens_out"
  | "cache_read"
  | "cache_write"
  | "reasoning"
  | "spent_usd",
  number
>;

type Aggregate = {
  run_count: number;
  attempt_count: number;
  totals: Totals | null;
  unknown_rows: UnknownRows | null;
  estimated_calls: number;
  oldest_queried_at: string | null;
  coverage: {
    runs_with_snapshot: number;
    missing_attempts: number;
    status_counts: Record<
      "available" | "partial" | "stale" | "pending" | "unavailable",
      number
    >;
    active_runs: number;
    refreshing_runs: number;
  };
};

type GroupRow = {
  key: string | null; // NULL only for an unknown historical model
  usage: Aggregate;
};

type Breakdown = {
  group_count: number; // before taking the first 20
  groups: GroupRow[];
  other: { group_count: number; usage: Aggregate } | null;
};

type AdminUsageResponse = {
  schema_version: 1;
  source: "gateway_api_logs";
  attribution: "grant_time_window";
  finalized: false;
  selection: {
    since: string;
    until: string;
    time_basis: "job_created_at";
    day_timezone: "UTC";
    model_basis: "job_configured_model";
    user_id: string | null;
    model: string | null;
    runtime: string | null;
    repo: string | null;
  };
  generated_at: string;
  summary: Aggregate;
  days: { date: string; since: string; until: string; usage: Aggregate }[];
  by_user: Breakdown;
  by_model: Breakdown;
};
```

`Aggregate` 的精确定义：

- `run_count` 是 distinct job 数；`attempt_count` 是 distinct attempt 数。多个 grant 行不能使它们增加。
- `runs_with_snapshot` 数的是单任务 `ledger.totals != null` 的任务，包括 0 调用、partial 和 stale 的有效快照。
- `missing_attempts` 是各 job `ledger.missing_attempts` 的和，不含尚未创建的 attempt。
- `status_counts` 每个任务按单任务 ledger 的最终状态恰好计一次，五项之和等于 `run_count`。无 attempt 的终态规则见 §3.2。
- `active_runs` 是 `job.state` 不在当前 `TERMINAL_STATES` 的任务数；`refreshing_runs` 是其单任务 ledger 的 `refreshing=true` 的任务数，两者不互相替代。
- 其余字段从有效 job ledger 汇总；状态与求和必须先在每个 job 内完成，再组合，不能让一个 job 的缺失把其他 job 的计数吞掉。
- 空集合：所有计数为 0，`totals/unknown_rows/oldest_queried_at=null`。只有 `run_count=0` 时显示 No runs；存在任务但无快照时显示 Unavailable。

### 4.2 分组和排序

- `days` 按日期升序，包含与选择窗口相交的每个 UTC 日。首尾日的 `since/until` 返回与窗口的交集，最多 91 个日桶。空日使用空 Aggregate。
- 用户/模型分组先覆盖全部选中任务，再各取前 20。按已知 `spent_usd` 的 Decimal 值降序，NULL 最后；同费用按完整分组键的 UTF-8 字节顺序升序，NULL 键最后。
- 余下的真实任务集合再做一次相同聚合，返回 `other`；不要用总计减前 20 计算 NULL 或状态。20 组以内 `other=null`。
- 页面说明“Top 20 by recorded cost”；费用未知的组不能被说成最便宜。`other` 显示“Other · N groups”，它不是可以按字面匹配的模型或用户。
- 不分页，不把持续变化的金额塞进跨请求分页游标。一次响应内三种分组分别与 summary 对齐；**不能把用户分组和模型分组再相加**。
- 超过 20 组仍可通过精确 ID/模型筛选定位特定组；完整导出与可遍历所有组的页面后续再做。

### 4.3 鉴权、错误和副作用

复用 `verify_admin_access` 和当前 session 权限，不新增管理凭据，不绕过实时权限检查。仅主 control-plane 挂载该 router，MCP listener 不挂载。

响应 `Cache-Control: no-store`。未登录、普通用户、权限撤销沿用现有 401/403 契约，拒绝后不执行聚合 SQL。不要为聚合读创建内容审计记录；它没有读取内容。

| 条件                   | 处理                                                                         |
| ---------------------- | ---------------------------------------------------------------------------- |
| 参数非法               | 422，安全的字段/窗口说明                                                     |
| 查询范围超过 §6 限额   | 422，`detail.error.type=usage_query_too_large`，提示缩小范围；不返回部分总额 |
| 数据库不可用或语句超时 | 503，`detail.error.type=usage_temporarily_unavailable`                       |
| 单份缓存报告无效       | 沿用 ledger 校验，计入缺失/状态，不把原始验证错误发给浏览器                  |

错误响应采用现有客户端支持的 `HTTPException(detail=...)` 形式，例如 `{"detail":{"error":{"type":"usage_query_too_large","message":"Narrow the time range or add a user or model filter.","code":422}}}`。复用 `fetchWithAuth` / `jsonOrThrow`，保留 `APIError.statusCode`，使 401/403 清屏继续生效；不另造错误解析器或把错误对象直接 stringify 给用户。日志不记录原始 snapshot、grant、prompt 或凭据。

## 5. 页面与 Runs 跳转

在 `AdminNavigation` 增加 Usage，地址 `/admin/usage`；沿用 Servers / Runs 的视觉语言和 `AdminRequired` 权限分支。

页面顺序：

1. **筛选**：Created（7/30/90 days 或明确起止时间，显示 UTC）、User ID、Configured model、Agent、Repository，Apply 和 Refresh。
2. **四个主数字**：Recorded cost、Model calls、Input tokens、Output tokens；数字上方保留 §2.1 的口径说明。
3. **覆盖情况**：`X of Y runs have a usage snapshot`，缺失 attempt 数和 partial/stale/pending/unavailable 任务数；有 active/refreshing 时说明缓存可能继续变化。其下显示 `generated_at` 为 Page read at、`oldest_queried_at` 为 Oldest known snapshot query，后者 NULL 显示 Unknown。细分 `unknown_rows` 与 `estimated_calls` 放在 Usage coverage 展开区，reasoning 缺失不制造核心指标警告。
4. **分组表**：Users / Configured models / Created date 三个 tab。用户和模型显示前 20 + Other；日期显示返回的所有日桶，可纯前端按每页 15 行展示。每行列为分组、Runs、Recorded cost、Calls、Input、Output、With snapshot、View runs。浏览器不重新求和。

金额显示最多 4 位小数，非零但小于 0.0001 的值显示 `<$0.0001`，真实零显示 `$0`；完整十进制值可在 tooltip/可访问的详情文本中查看。格式化不改 API 原值，不能经浮点先舍入再聚合。计数沿用 `en-US` 分隔格式；日期明确 UTC，不能混用本机时区来决定分桶。

缺失值显示 Unavailable/—，不渲染为 0。没有有效费用的日桶不画“零消费”图；本期表格即可，不新增图表库。

### 5.1 筛选与更新行为

- 第一次进入使用最近 7 天，之后把已应用的**绝对** `since/until` 和筛选写入 URL；可复制、刷新与浏览器前进/后退。写回地址栏必须保留当前 pathname（部署可能带 `NEXT_PUBLIC_BASE_PATH` 前缀），不能硬编码 `/admin/usage`。
- URL 在挂载期间变化（浏览器前进/后退、同路径 query 导航）必须按新选择重新读取；只处理"首次打开深链"不算满足。
- 时间窗是唯一权威：`Created` 控件显示**实际应用**的窗口，与预设长度相同但锚点不同的窗口（例如一周前结束的七天）显示 Custom，改其它筛选不得把它重新锚定到今天。
- 一切以窗口换时长的地方（包含 Refresh）必须从**实际窗口**计算时长，不能把控件当前值当数字解析：控件会显示 `custom`，而 `Number('custom')` 是 `NaN`，会直接抛 `RangeError: Invalid time value`。
- Apply 的身份筛选**只来自表单**：清空某个字段必须让它从下一次请求中消失，不能保留旧值。
- 从 URL 恢复或应用过一次后，窗口就是绝对值。只改 User ID 等非时间筛选再 Apply 时保留窗口；只有用户明确选择时间预设后再 Apply，才重新锚定到当时的当前时间。自定义窗口/日期深链在控件中显示 Custom，不能仍写 Last 7 days。Refresh 保留已应用窗口与筛选，只重新 GET 缓存。
- 首次加载用紧凑骨架；首次失败显示重试。相同筛选的 Refresh 失败可保留旧结果，明确显示失败和原 `generated_at`；更换筛选先清旧数据。
- 用 `AbortController`/请求身份丢弃过期响应。账号变化重新挂载；401/403 立即清空旧数据并显示权限状态，迟到响应不能恢复它。
- 无任务、任务无快照、部分缺失、缓存刷新失败是不同状态，均需可见的测试样例。
- 切换筛选（任务集合变化）与同条件 Refresh（重读缓存）必须区分：前者不得让旧结果与新筛选并存——旧数字配新跳转条件会把管理员带到与数字无关的任务集合；后者可以保留旧结果及其 `generated_at`。
- 链接不可用时（缺一端、倒置、超 90 天、含本页不支持的参数）显示筛选错误并且**不发请求**，不静默退回默认窗口或忽略未知参数：那会回答一个没人问过的任务集合。
- 无效链接期间**不得把默认筛选写回 URL**。Next 会同步 `replaceState`，写回会替换掉正在报错的那个链接、触发重新挂载并让错误消失，随后按默认窗口查询所有用户——即"报错"与"照常查询"同时发生。只有用户主动 Apply 才写入新的筛选。
- "无效"必须是**状态而非常量**：`Apply` 要能清掉它，写入并查询用户刚选定的窗口。若把无效判定固定在挂载时那条 URL 上，守卫会连 `Apply` 一起挡死，页面变成一个无法恢复的死状态——这比原来的"静默扩大"更难察觉。

### 5.2 View runs 必须实际生效

当前 `web/src/app/(agents)/admin/runs/page.tsx` 总从 `windowFilters(7)` 初始化，**没有读取 URL query**。因此本期需要小幅修改 Runs，解析白名单参数 `since/until/user_id/model/runtime/repo`，同时保留其既有 state/host 筛选。

- 用户行：保留全部已应用筛选，加该行 `user_id`。
- 模型行：保留全部已应用筛选，加该行 `model`。
- 日期行：保留全部筛选，以该日与原窗口交集替换 `since/until`。
- Other 行不提供伪造的精确筛选链接；未知历史模型行同样不提供 View runs，因为现有 Runs 没有 NULL 模型筛选语义。不要把 NULL 编码成字符串 `null`。
- 数据库历史 user/model 键可能超过筛选接口 256 字符上限；这种键仍原样分组并参与总计，可省略显示但不截断身份。禁用该行的精确跳转并解释原因，不生成必然 422 的链接，不为此扩张现有 Runs 契约。
- Runs 直接打开深链、Refresh、Load more 和浏览器返回都必须保持精确窗口。现有游标/后端时间窗语义不变；用户主动换预设再 Apply 才重置窗口。
- Usage 页面和 Runs 初始参数无效时显示筛选错误，不静默退回所有用户或默认窗口。移除 `cursor` 后再切换筛选。
- Next.js 的 `useSearchParams` 按当前项目构建要求放进合适的 Suspense/客户端边界，并跑 production build，不能只通过组件 mock。

### 5.3 修订 2026-09-28：页面改为用量看板

上线后的页面按数据结构排布：筛选表单、四张数字卡、Usage coverage 卡片、带 With snapshot 列的分组宽表。它没有直接回答管理员来这里要问的三件事：花了多少、谁在用、在涨还是在跌。本次只改页面，接口、账本口径和求和规则都不变。

页面顺序改为：

1. **工具栏**：与 Activity 相同。时间窗为最近 7/30/90 天；链接带来的窗口显示 Custom 和起止日期。已应用的身份筛选显示为可移除的标签。右侧是 Open in Activity（带上整个选择）和刷新图标。不再有手填 ID 的表单：点排行里的用户或模型来缩小选择；链接仍可带入 `user_id/model/runtime/repo`。
2. **一行主数字**：Cost、Turns、Model calls、Input tokens、Output tokens。一万以上的计数缩写为三位有效数字（`87.3M`）；金额显示到分（`$15.64`），非零但不足一分显示 `<$0.01`；完整值都在 tooltip。每个数字下方是与上一个等长窗口的变化。它由前端对上一个窗口、相同身份筛选再读一次得到，读取失败时只是不显示变化。Input tokens 旁显示缓存读取占比，只在两项都完整上报、且缓存读取不大于输入时显示。
3. **Not final 一行**：只在数字不完整时出现，列出运行中的 turns、没有完整计入的 turns（状态不是 available）以及没有记录费用的调用。各状态的细分、估算调用数和最旧一次缓存读取时间放在 tooltip。它取代 Usage coverage 卡片。
4. **每日柱状图**：取代 Created date 表，按返回的日桶作图，可在 Cost / Turns / Model calls / Input tokens 间切换。悬停或聚焦时读出当天数值；点击进入该日与窗口交集的 Activity。只有部分落在窗口内的首尾两天颜色较浅。有 turns 但没有记录值的日子画一条虚线并读作 “no usage recorded yet”，不画成 0。图用 CSS 画，没有新增图表库。
5. **用户、模型两个排行**：取代分组表。每行显示完整 ID、占比条、turns 和费用。占比按费用计；选择没有费用时按调用数，再没有则按 turns。点一行会把整页缩小到这个用户或模型，行尾箭头进入 Activity。Unknown model、超过 256 字符的键和 Other 行不提供筛选，原因写在该行 tooltip。

与 §2.1、§5、§5.1、§8.3 有意不同的地方：

- §2.1 的固定说明改为时间窗控件的 tooltip，页面上不再有说明段落。
- 金额显示从最多 4 位小数改为到分。精确值仍在 tooltip，reducer 和 API 都不变。
- Refresh：本页选定的预设窗口会重新锚定到当前时间，与 Activity 一致。“最近 7 天”点刷新应包含刚创建的 turns，而不是停在打开页面的时刻。因此这次读取是新的选择，读取期间不保留旧数字。从链接进入的窗口仍按原绝对窗口重读，刷新失败时保留旧数字并说明。§8.3 的 “Refresh 不改变绝对窗口” 现在只约束链接窗口。
- 无效链接改由选择一个时间窗来替换，原来是 Apply。
- 文案使用 session / turn / step 的叫法（见 [2026-09-17 导航文档](2026-09-17-admin-conversation-navigation.md)），runs 改为 turns。

实现注意：页面把选择写回地址栏后，Next 会把这次 `replaceState` 作为新的 query 送回页面（§5.1 已记录）。页面记住自己写入的 query，只有不是自己写入的 query（浏览器前进后退、点 Usage 标签）才重新挂载。否则每次缩小选择都会重新挂载并多读一次。

## 6. 后端实现与规模边界

### 6.1 数据读取

新增专用 `AdminUsageQueries`，借用 Agent 现有连接池；不循环调用 `get_admin_run()` 或 owner API。单次 `selection()` 在一个 `repeatable_read`、只读事务内完成下列三步，沿用 Admin queries 的 5 秒 `statement_timeout`：**任务元数据必须与账本快照来自同一个读取视图**，否则会出现数据库从未存在过的组合（例如任务仍计为 running，费用却已取其结束时的快照）。实现分两次事务读取会在真实 PG 上稳定复现该错误。

1. 先以创建时间和精确筛选查询轻量任务集合，最多取 10,001 个任务用于判定超限。只投影 id、user_id、任务配置 model/runtime/repo、state、created_at；不借用含 `tool_calls` COUNT 的 `_RUN_COLUMNS`。**身份字段按原样返回，不做 `LEFT()` 截断**：截断会把共享前缀的不同用户或模型并成一组，还会生成数据库匹配不到的跳转参数；显示长度由前端控制，筛选契约长度之外的键禁用跳转。
2. 对这批 ID 先在 PG 内测量行数与快照字节，超预算整次拒绝、不投影任何 payload；再批量 LEFT JOIN attempts 与 ledger，取 ledger 汇总实际需要的字段，保留无 attempt/no-ledger 情况。账本引用列名必须与共享 reducer 读取的 `id` 一致，否则「已有引用、等待快照」会被误判成不存在引用，聚合的 pending/unavailable 与单任务页面不一致。`refreshing` 判定与现有 `ledger_rows_for_job()` 完全一致，不另猜刷新阶段。
3. 所有行读完后释放事务，再在 Python 按 job 运行共享校验/求和，得到 summary 与三组分桶。每个 job 的记录只解析一次，四个归约复用同一批结果。分组所需数据已经在同一读取视图内；处理时不再读库。

可以用少量固定查询或一个受限 CTE 实现，查询次数不能随任务数增长。数据库访问层若已被 #155 拆为 ports，通过合并后现有的只读查询入口取得连接；不要为沿用示例而恢复旧 `_pool` 依赖。

只投影账本报告的协议白名单字段：schema_version、source、grant_id、external_job_id、attribution、window、as_of、finalized、totals、unknown_rows、estimated_calls；totals/unknown_rows/window 的内部键也按契约白名单取。grant 标识只在服务端用于已有校验，不进入聚合返回体。不取绑定凭据、原始 metadata、事件表或任意 payload。

投影必须保留字段是否存在及其 JSON 类型：只拷贝原来存在的白名单键，缺键仍缺，显式 NULL 仍为 NULL。不能用 `jsonb_build_object` 把缺失的 required-but-nullable 键补成 NULL，从而把坏报告修成有效报告；也不对字符串数字、bool、非 object 强制转换或归零。损坏快照保留“无效报告”语义。若公共校验/求和需要小幅提取，为 owner/Admin Run/聚合同时建立相等性测试。

### 6.2 明确拒绝过大查询

首版限额是保护现有控制面，**不是已测出的性能承诺**：

- 时间 ≤90 天；任务 ≤10,000；attempt/ledger LEFT JOIN 输出行 ≤50,000。
- 已投影快照的累计序列化输入 ≤32 MiB。预算在 Postgres 内先量后读，不能先无限 `fetch()` 再检查内存大小。
- 响应 HTTP JSON ≤1 MiB，**按实际序列化后的 UTF-8 字节检查**：行数与快照预算都约束不了「计数器本身很宽」的合法报告，只有对真正要发出的响应计数才成立。超限同样整次 `usage_query_too_large`，不裁掉日期或部分分组。
- 任一限额触发，整次查询返回 `usage_query_too_large`；不采样、不丢掉大任务、不返回看似完整的前 N 个任务总额。Top 20 只限制分组展示，不限制总计覆盖。
- 本期不预先创建 rollup、索引或缓存服务。用真实 PG 的 `EXPLAIN (ANALYZE, BUFFERS)` 记录数据规模、查询次数、计划和延迟；只有实测瓶颈需要时才另提最小索引迁移并解释理由。

本地评估至少包含 3,000 个任务、多 owner/model、多 grant 行、旧任务缺失和大量**无关事件行**。聚合不能因事件表变大而扫描它。再用边界数据验证 10,001 任务、50,001 行和输入/响应字节超限均明确失败。

## 7. 文件与实施顺序

| 位置                                                  | 修改                                                                   |
| ----------------------------------------------------- | ---------------------------------------------------------------------- |
| `cloud_agent/admin_usage.py`（新）                    | 聚合读模型、参数验证和纯分组函数                                       |
| `cloud_agent/storage/admin_usage_queries.py`（新）    | 批量任务与快照投影、事务、限额                                         |
| `cloud_agent/api/admin_usage.py`（新）                | 单一 GET 路由、鉴权、错误和 no-store                                   |
| `cloud_agent/app.py`                                  | 只在主控制面注册 router                                                |
| `cloud_agent/ledger.py`                               | 仅必要的共享求和/无 attempt 语义提取，保持 owner 与 Admin Run 返回不变 |
| `web/src/lib/api/adminUsage.ts`（新）                 | 类型、鉴权 GET、错误处理、取消                                         |
| `web/src/app/(agents)/admin/usage/`（新）             | 筛选、总计、覆盖率、分组表及测试                                       |
| `web/src/app/(agents)/admin/AdminNavigation.tsx`      | 增加 Usage                                                             |
| `web/src/app/(agents)/admin/runs/page.tsx` 与测试     | URL 筛选初始化和精确窗口保持                                           |
| `tests/unit/test_admin_usage.py`（新）                | 聚合与现有单任务账本口径一致                                           |
| `tests/servers/test_admin_usage.py`（新）             | 路由、权限、边界、无副作用                                             |
| `tests/integration/storage/test_admin_usage.py`（新） | 真 PG 多任务查询和同事务一致性                                         |

建议一个聚焦的实现 PR，目标 `dev`：先完成纯模型/测试，接批量查询/API，再完成页面和 Runs 深链，最后跑验收。本文档没有授权自动合并实现 PR。

实施前同步最新 `dev`。#149 的 schema baseline 和 #155 的 storage ports 正在并行修改底层布局，本设计不依赖它们先合并，也不要求保持旧文件结构。以当时已合并的存储接口和 schema 为准，保留本文的统计语义与测试；不要编辑待删除的迁移链或顺手搬动存储模块。

## 8. DS 的验收清单

### 8.1 账本与聚合

- [ ] 至少两个 owner、两个配置模型，多个 UTC 创建日；总计 = 全部日期组之和 = 用户前 20 + Other = 模型前 20 + Other。按已知值求和规则比较 NULL，而不是把 NULL 当 0。
- [ ] 先验证每个任务摘要与现有单任务接口 ledger 相同，再验证各组包含的任务集合；不能只用新实现自己生成期望值。
- [ ] 多 attempts、多 grant、同 grant 跨不同任务、取消/失败有消费、Fork 共享 sandbox 均不漏算或重算。
- [ ] 无 attempt 的活跃/终态任务，无引用的历史 attempt，坏 schema/错 grant，valid+missing 的混合任务。
- [ ] 0 调用有效快照、全部未知、部分已知、reasoning-only 缺失、estimated_calls、含 cache 的输入 token、极小费用和高精度 Decimal。
- [ ] last_error 但保留旧有效 snapshot；future/due refresh 的语义保持；刷新不叠加旧快照；五分钟后 finalized 仍为 false。
- [ ] 一轮跨 UTC 日和过滤结束时间；创建时间恰好等于 since 时包含、等于 until 时排除；带非 UTC offset 的等价输入得到相同任务集。
- [ ] 相同金额的稳定排序、NULL 金额、21+ 用户/模型、Unknown model 与 Other 区分；一个任务在每个维度内只出现一次。超长完整键不合并且不生成无效深链。

### 8.2 接口与查询

- [ ] 管理员可跨 owner 查询；匿名/普通用户/权限撤销被拒绝；拒绝后不执行聚合查询；MCP listener 无路由。
- [ ] 在 prompt/title/metadata/事件/报告额外字段放 canary；默认查询与响应不读取/泄露内容，返回体不含 grant_id、usage_id、凭据或错误原文。
- [ ] 没有事件表扫描、每 job SQL、逐用户身份查询、新增 Gateway ledger 请求、queue refresh 或执行状态写入。现有鉴权请求保留。
- [ ] 真 PG 验证事务读取一致性：在受控并发更新 snapshot 的情况下，summary 和各分组仍来自同一读取视图。
- [ ] 无效/未知筛选、超量任务/行/字节、超时/断库、安全错误文案和 no-store；超限绝不返回部分成功总额。required nullable 字段的缺键和显式 NULL 在白名单投影后仍能区分。

### 8.3 页面、质量门与 staging

- [ ] Usage 导航可见且权限正确；首次加载、无任务、无快照、部分缺失、刷新失败、过滤错误均有测试。
- [ ] 账号切换/撤权清空数据；快速筛选的旧响应不能覆盖新结果；Refresh 不改变绝对窗口。
- [ ] Users/Models/Days 的 View runs 真正筛中同一集合；日期链接使用日交集；直接打开、后退、刷新、Load more 均不丢筛选。
- [ ] 不将 NULL/小额费用画成真实 0；不显示“实际服务模型”“按请求日消费”“实时用量”“已结算”等超出数据能力的文案。
- [ ] 金额精确性：混合小数位（`0.01` + `0.00012345`）、极小值 + 大值、跨 job 拆分后的总量，reducer 返回值与独立 `Decimal` 计算一致且不因分桶方式改变。
- [ ] 同一读取视图：把**已选中**任务的 state 与快照在同一请求内一起更新，返回的 `(active_runs, spent_usd)` 必须等于更新前或更新后之一，不能是混合值。
- [ ] 有引用无快照、无引用、无 attempt、坏快照四种情况的 status/missing_attempts 与单任务接口逐项一致。
- [ ] 共享前缀的长 model/user 不被合并，且其 key 超过筛选契约长度时不生成跳转。
- [ ] 响应字节上限按序列化结果触发并整次拒绝。
- [ ] UI：清空身份筛选、一天深链改 Status、切换筛选时旧数字与新链接不共存、URL 在挂载期间变化、带 base path 的地址栏写回。
- [ ] `make check`；新 DB 测试和 `make test-db`；Web format/lint/typecheck/vitest/production build 按仓库现有脚本通过。
- [ ] PR 描述记录实测数据规模、SQL 计划/延迟和质量门结果，不沿用旧 PR 的测试数字。
- [ ] 合并后按既有流程部署 staging，核对实际 API/web SHA 和 CI 部署结论，再用浏览器打开 Usage 验收。
- [ ] 对 staging 选定任务集，逐任务现有 ledger 与聚合逐字段对齐；抽查跨 owner、失败/取消和无账本任务。记录查询时间，避免把快照更新导致的跨请求变化误判为算错。
- [ ] staging 的权限检查使用现有合法 session，不创建生产数据；不为了凑测试覆盖编造线上快照。staging 数据缺少的对抗案例由本地真实 PG fixtures 验证并写明。

完成标准：管理员能从 Usage 得到口径明确、覆盖缺口可见的跨任务总量，并通过 View runs 找回完全一致的任务集合；现有 owner 与 Admin Run 账本定义保持一致。

## 9. 后续边界

本期落地后，服务器容量是独立 PR；逐请求模型/延迟和按请求时间计费需要 Gateway 明细接入；共享沙箱整轮资源需要补采集和归属。它们不能通过改这个聚合页的标签或相加现有进程峰值来替代。
