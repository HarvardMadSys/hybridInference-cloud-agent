# Admin → Servers：服务器容量 v1

状态：本地设计稿，供实现；本次不提交、不建 PR、不部署。

范围更新（2026-09-11）：按产品讨论，将 CPU / 内存利用率折线图纳入本期，默认最近 1 小时，可切换 6 小时 / 24 小时。当前值每 30 秒更新；历史每分钟保存一次观察，保留 24 小时。

代码基线：`dev b7a24c610a1185b0b288dce32eabd8fe487d710f`，2026-09-11 核查。Agent 仓库为 `HarvardMadSys/hybridInference-cloud-agent`。本文不以 #168 是否合并为前提；它只扩 Servers，不改 Usage 的统计定义。

## 1. 交付目标

管理员打开 Servers，应当直接回答：**哪台服务器正在承受负载、是短时升高还是持续吃紧、还有多少执行配额、多少沙箱仍被保留、这些数据有多新。**

本期交付三组并列事实：

| 信息 | 主展示 | 含义 |
|---|---|---|
| 整机 CPU / 内存 | `CPU 37% · 32 logical CPUs`；`Memory 33% · 42 / 128 GiB`；展开看两张利用率折线图 | Docker 宿主 Linux 的整体负载，含 Agent 之外的进程；曲线帮助区分短时升高与持续压力 |
| 并发执行配额 | `2 / 3 in use · 1 free` | 当前执行环境的 claim 配额；不能保证还能启动一个新 VM |
| 保留沙箱 | `3 running · 7 stopped`，必要时补其他状态和部分覆盖标记 | 当前实际可见的 Agent 容器；一次 Fork 不多算一个 VM |

剩余内存不能直接除以默认 `4g`，CPU 也不能除以默认 `2 CPU` 来推算“还能接 N 个任务”。沙箱限制不是资源预留，已有会话可能复用同一容器，而且同机可能运行其他服务。

本期展示当前快照和最近 24 小时的 CPU / 内存曲线。不做趋势预测、自动扩容/调度/Drain、磁盘清理、GPU 推理容量、单次执行排障、逐请求账本、整轮资源归因、告警或论文导出。磁盘容量另做：必须先明确 Docker data-root 所在文件系统，不能用 runner 容器 `/` 的大小代替宿主磁盘。

## 2. 现有代码与需要补的链路

| 现状 | 证据与影响 |
|---|---|
| 已有 host 注册、每 30 秒心跳、admin hosts API | `cloud_agent_host/enrollment.py`；`cloud_agent/api/hosts.py`；`cloud_agent/host_pool.py`。复用现有认证和传输，不新建服务 |
| 心跳没有 CPU / RAM / 容器数量快照字段 | `cloud_agent/schemas.py::HostHeartbeatRequest`、`enrollment.py::heartbeat()`、`storage/store.py::record_host_heartbeat()` 都需要扩展 |
| `slots_used` 由 claim 原子增加，attempt 结束或被回收后减少 | `schemas.py:1614`；`store.py::claim_job()` / `_release_host_slot()`。保留原语义；已过期但尚未回收的 attempt 仍占额度 |
| 一 group 对应一个 `agent_sandboxes`，Fork 增加 membership | `store.py:134`；baseline migration 的 group/membership 表。不能按 thread 或热 residency 数计算 VM 数 |
| 默认三个 runner 副本共享一个 enrollment | `deploy/host/docker-compose.sandbox.yml:274`。多个心跳是同一个 host 的多次观察，不能相加 |
| Servers 按 `labels.physical_host` 分组，再选一个执行环境代表 | `web/src/app/(agents)/admin/hosts/format.ts::serverInventory()`。旧执行环境仍可能保留容器，不能只看代表行的负载 |
| 现有 `resources.py` 是单容器/进程资源统计 | 不作为整机数据源；本期不依赖 Kata guest eBPF |

推荐链路：

```text
宿主只读 proc 文件 ── CPU / RAM ──┐
                                ├─ runner 容量快照 → 现有 host heartbeat
Docker 单次只读 list ── 容器清点 ─┘                      ↓
                                      agent_runner_hosts 最新快照
                                          ↓                         ↓
                          现有 runner-hosts API          每分钟历史采样任务
                                          ↓                         ↓
                                   Servers 当前值           24h 有界历史表
                                                                    ↓
                                                      单服务器历史 API → 折线图
```

浏览器轮询只读 Postgres，不触发 Docker、SSH、网关请求或 broker reconciliation。

## 3. 数据口径

### 3.1 整机 CPU 与内存

观测对象固定为**运行 Docker Engine 的 Linux 宿主**。Kata VM 的 CPU/RAM 活动自然包含在宿主负载里，但不在本期拆出归属。Docker Desktop 的观测对象是其 Linux VM，不能称为 Mac/Windows 整机。

在可信 runner 容器中增加固定文件的只读 bind：宿主 `/proc/stat`、`/proc/meminfo`、`/proc/sys/kernel/random/boot_id` → `/run/host-metrics/` 下对应文件，`create_host_path: false`。不挂整棵 `/proc`，不加 `pid: host` 或 privileged，不把这些挂载传入任务沙箱。

collector 默认只读这些显式路径。缺文件、权限错误或无法确认是相同 Docker 宿主时返回缺测，不能回退读取一个未知范围的 `/proc`。当前支持本地 Unix Docker socket；remote Docker / rootless 或其它宿主视图需单独适配后才能声称支持。

CPU 用同一 runner 进程内相邻采样计算，通常间隔 30 秒：

```text
total = user + nice + system + idle + iowait + irq + softirq + steal
busy_percent = 100 × (Δtotal − Δidle − Δiowait) / Δtotal
```

这是全机归一化的非空闲比例，不含 I/O wait，包含 steal；不再乘 CPU 数。`guest` / `guest_nice` 已包含在 user / nice，不重复加入分母。首样本、boot id 改变、CPU 数变化、计数回退、分母不正或间隔超过 90 秒时，CPU 值为 NULL，重建基线。内存不必等 CPU 第二个样本。

内存：`used_bytes = MemTotal − MemAvailable`，`used_percent = 100 × used_bytes / MemTotal`；表格同时展示利用率和已用/总量，展开保留可用量。MemTotal 必须大于 0，kB 转 bytes 使用 1024。MemAvailable 缺失时不改用 MemFree 冒充。当前值和折线图使用同一个计算 helper，不按各自显示精度重复推算。依据：[Linux proc 文档](https://docs.kernel.org/filesystems/proc.html)、[内核 guest accounting](https://github.com/torvalds/linux/blob/master/kernel/sched/cputime.c)。

### 3.2 沙箱清点必须是只读 Docker inventory

**不要复用 broker `/sandboxes` 或 `SharedChatSessionSupervisor.list()` 来采容量。** 当前 `list()` 会调用 `reconcile()`；它可能停止孤儿容器、清理网络，并返回包含旧 group 的内存缓存。`ChatSandboxManager.list()` 也包含逐容器 inspect。它们用于生命周期协调，不能当成无副作用的轻量容量探测。

新增一次独立 `docker container ls --all --no-trunc`，只筛 `org.freeinference.chat-sandbox=true`，用 JSON Go template 输出四个字段：完整 container ID、namespace label、sandbox ID label、`.State`。参数使用数组，不走 shell，不使用 `--size`、inspect、stats、reconcile、stop 或 prune。[Docker 支持的列表字段](https://docs.docker.com/reference/cli/docker/container/ls/#format-the-output---format)。

- 严格匹配本 runner 的 `AGENT_SANDBOX_NAMESPACE`；非空 foreign namespace 不计入本份数量。
- **按完整 container ID 去重计实体**，而不是 membership。一个 Fork 共用一容器，只计一次。同一 sandbox ID 如果意外有两个容器，两者都占资源，不能合并成一个；可带 `duplicate_sandbox_id` 提示。
- `running` 归运行；`exited` 归已停止；`created / paused / restarting / removing / dead` 归其他状态并保留固定枚举分解。paused 不能宣称已经释放内存。
- 未带 namespace 的历史 chat 容器无法安全归属。识别到时标 `partial / unscoped_legacy_containers`，数字只代表已明确归属的下界，不计入某个猜测的部署；不为消除缺测而修改或删除容器。
- 不从 running 推导“正在执行任务”，闲置会话也可保留运行中的容器。不从 stopped 推导“已释放全部资源”，它仍可能占磁盘。

本期不展示“busy / idle sandbox 数”或“可 Fork 成员额度”，避免把终端活动、会话保留、成员槽位与 host 并发额度混在一起。

### 3.3 执行配额与可接活状态

继续使用原 `slots_total / slots_used`。上限为数字时，空位是 `max(total − used, 0)`；上限为 NULL 显示 `Limit not set`，空位未知。不能过滤掉过期 lease 后改算一个更好看的 used 数，调度器尚未回收的额度确实不可用。

主表配额对应**当前代表执行环境**，旁边可展开查看同一服务器的全部 enrollment 配额。不要把多个执行环境的配额总和包装成物理机器的安全容量。

online、draining、offline、当前 execution profile 与 new-chat pin 继续按已有策略显示。draining 可以同时存在心跳过期标记；心跳新鲜也不代表空位大于零。文案用“执行配额”，不显示一个混合 CPU/RAM/配额计算出的 `Can accept N jobs`。

## 4. 采样与协议

### 4.1 每个副本独立采样，失败不影响执行

新增 `cloud_agent_host/host_capacity.py`，以可注入的文件 reader、时钟和 Docker 命令 runner 实现。各副本保留自己的 CPU 基线，30 秒一次；控制面只存一份最新收到的独立报告，不合并副本原始计数器。

采样可在现有 heartbeat 周期调用有界 collector，或用一个只保留最新结果的 daemon thread；**本设计推荐后者**，使 Docker 延迟不增加心跳耗时。每个进程最多一个采样任务，不排队补发。发送时读取内存快照并计算其 monotonic age，首次未就绪允许不带 capacity。

采样预算：整个 Docker 子进程 ≤2 秒，stdout 流式累计 ≤1 MiB / ≤4096 行，stderr 有界。超限立即终止并回收子进程，不能先完整读入再声称受内存预算保护。超时、非零退出或畸形输出使本次 inventory 缺测；CPU/RAM 仍可报告。proc 文件也有读长上限。采样不增加任何生命周期 RPC。

既有 retained-sandbox heartbeat 仍保持其协调职责。其客户端当前使用 180 秒 timeout，超过 90 秒离线阈值；这不是可复用的容量采样预算。实现时至少保证新 collector 不调用它、不共享其阻塞任务；若 staging 验证发现这条既有回调使 liveness 停顿，单独以 heartbeat 专用短超时/至多一个未完成读取修复，不能修改执行 RPC 的 timeout 或把超时当空容器列表。

### 4.2 可选 `capacity` 字段

扩展现有 `POST /v1/agent/host/heartbeat`，不新增 host 认证入口。以下为建议 v1 形状，数值是示例：

```json
{
  "capacity": {
    "schema_version": 1,
    "sample_id": "00000000-0000-4000-8000-000000000001",
    "sample_age_ms": 120,
    "linux_boot_id": "00000000-0000-4000-8000-000000000002",
    "source": "linux_host_proc_and_docker_list",
    "cpu_percent": 37.2,
    "cpu_logical_count": 32,
    "cpu_window_seconds": 30.01,
    "memory_total_bytes": 137438953472,
    "memory_available_bytes": 92341796864,
    "inventory": {
      "namespace": "example-agent-kata",
      "status": "available",
      "running": 3,
      "stopped": 7,
      "other_states": {"created": 0, "paused": 0, "restarting": 0, "removing": 0, "dead": 0},
      "reason": null
    },
    "missing": {"cpu": null, "memory": null}
  }
}
```

约束：

- `sample_id` 为 UUID，`linux_boot_id` 为 UUID 或 NULL；boot id 读不到时 CPU/RAM 缺测，不猜宿主范围。inventory 可独立成功并按执行环境显示，但未知 boot id 不允许跨环境求和。
- CPU 是有限数且在 `[0,100]`，logical count 为正整数；CPU 缺测时 percent/window 为 NULL，原因从 `warming_up / counter_reset / source_unavailable / invalid_sample` 中取。
- 内存 total 必须大于 0，available 非负且 ≤ total；需要的字段缺失时整组 NULL。bytes 为 JSON 安全整数，不能输出 NaN、Infinity 或字符串预览。
- inventory `status` 为 available / partial / unavailable；不可用时计数字段为 NULL，成功空列表才为 0。未知状态/无法安全归属可为 partial，必须保留原因；超时、超限、解析失败为 unavailable，不返回截短后的较小总量。
- `sample_age_ms` 是发送前按 monotonic clock 算出的观测年龄，为 `[0,300000]` 的整数；更老的缓存不发送。重试重新计算，不能复用序列化时的旧 age。不得把缓存样本用当前时间重新盖章。
- capacity JSON 实际 UTF-8 大小 ≤8 KiB；仅允许固定字段和固定状态 map，不上报容器清单、命令、环境变量、用户标识、路径或工具内容。
- 旧 runner 省略 capacity 合法。未知版本/格式错误只丢弃这个可选容量块，**不能令原本合法的 liveness heartbeat 422**；保留已有必需字段的认证与校验。使用独立的 schema 解析/归一化边界，不能让嵌套模型异常逸出后阻断整个心跳。

### 4.3 存储与新鲜度

给 `agent_runner_hosts` 增加 `capacity_snapshot JSONB NULL`、`capacity_received_at TIMESTAMPTZ NULL`、`capacity_sampled_at TIMESTAMPTZ NULL`，另增服务端生成的 `capacity_epoch UUID` 作为本次登记的容量数据代次；使用新 Alembic revision，不编辑已存在的 baseline。sampled_at 是服务端用 received_at 减去上报年龄得到的观测时刻估计，不使用 runner 的墙钟。现有行在迁移时获得 epoch，之后只有重新 enrollment 才轮换；不能用 credential digest 当历史标签。

同一次写入保存整份快照和两个时间。相同 sample_id 不刷新时间；交错到来的旧缓存也不覆盖更年轻的已存观察，用服务端归一化后的 sampled_at 比较。省略、坏版本、非法块不刷新。重新 enrollment 时清空三列并轮换 epoch。仅按认证得出的 host 写入，body 不能指定另一个 host。

容量更新另需一个很窄的并发保护：在 SQL 内确认 host 仍持有本次认证的 credential digest；失配只丢弃容量字段更新。鉴权发生在依赖中，仅按 host 名写入会允许“旧凭证 A 已鉴权 → 同名重新 enrollment B 并清空 → A 迟到写回旧容量”。digest 只在内部认证上下文传递，不进入快照/响应/日志。本期不借此改造调度或凭证生命周期。

定义显示年龄：

```text
capacity_sampled_at = capacity_received_at − sample_age_ms / 1000
sample_age_seconds = max(0, now_control_plane − capacity_sampled_at)
freshness = fresh（年龄 ≤90 秒） / stale（>90 秒） / unavailable（从未有合法样本）
```

这是最近收到的观察，时间精度仍受传输延迟影响，不声称各机器同瞬间采样。`last_heartbeat_at` 不能替代容量时间：旧 runner 或失效的 collector 可以继续正常心跳。

过期值仍可作为“最后观察”灰显，必须带年龄；不能用它显示当前绿色负载或当前沙箱总量。一个 CPU 字段 warming_up 不应隐藏成功的 RAM/inventory；整份快照的时效与各字段是否缺测分开处理。

### 4.4 历史：每分钟一份观察，保留 24 小时

折线图必须读取服务端持久化的历史；关闭浏览器仍继续采集，刷新页面也能看到已有曲线。复用 Agent Postgres，不新增监控服务。

新增 `agent_host_capacity_history`，只存 CPU / 内存标量，不复制 inventory 或 8 KiB 心跳 JSON：

| 字段 | 用途 |
|---|---|
| `host`、`capacity_epoch` | 当前 enrollment 的数据归属；host 外键随 Forget 硬删除，epoch 不作级联更新 |
| `bucket_at`、`sampled_at`、`recorded_at` | sampled_at 向下取整到 UTC 分钟作为 bucket；保留真实观察时间和写入时间，不能用写入时间伪装采样时间 |
| `sample_id`、`linux_boot_id` | 防止旧快照重复入历史；识别宿主重启并断开曲线 |
| `cpu_percent`、`cpu_logical_count`、`cpu_window_seconds` | 原始有效 CPU 观察或 NULL，不在入库时显示舍入 |
| `memory_total_bytes`、`memory_available_bytes`、固定缺测原因 | 按同一公式生成百分比；缺测与实测 0 区分 |

主键为 `(host, capacity_epoch, bucket_at)`，另对 `(host, capacity_epoch, sample_id)` 唯一，过期清理索引为 `bucket_at`。**每分钟保留第一份被历史任务选中的新鲜观察**，冲突不替换、不相加、不求多副本均值。相同 sample_id 不能生成第二个历史点。CPU 缺测但内存有效仍保留该点，CPU 图在该点断开。

在 `cloud_agent/app.py::lifespan(background=True)` 增加独立的容量历史 task，沿用现有任务的启停/取消方式；MCP listener 的 `background=False` 进程不启动它。每 60 秒从已保存快照中读取年龄 ≤90 秒的观察，保存其原 sampled_at 和读取时的 epoch，不能写入时再用最新 epoch 给旧样本盖章。按 host 分页，每批至多 100 行，只投影需要的标量；每批数据库工作有 2 秒 timeout，采样单轮总预算 10 秒，超过预算保留游标下轮继续，不能不断从第一页开始饿死后面的 host。异常记录后进入下轮，不积压、不补造错过的分钟。即使误启多个 task，唯一约束也保证不重复。

这个后台 task 与心跳请求、lease reaper 独立。历史读取/写入超时不能使 host 掉线或阻止任务回收。重新 enrollment 后图表只查询当前 epoch；后台任务即使迟到写入旧 epoch，也不会污染新机器的曲线。宿主仅重启而未重新登记时保留同 epoch 历史，但 boot id 变化处断线。现有 Remove 只是禁用 host，历史仍可看；Forget 硬删除 host 时历史级联删除。

每轮至多清理一批 2,000 条早于 24 小时的历史，独立短事务、超时后下轮继续。查询始终过滤到最近 24 小时；物理保留为“24 小时 + 有界清理延迟”，停机恢复后分批清理积压，不能跑无界 DELETE 或拖慢 reaper。历史连续清理失败要记录可检索的运维日志，不能悄悄无限增长。

每个连续观测的 enrollment 每天最多约 1,440 行；100 个 enrollment 约 144,000 行/天。实际磁盘、索引与查询耗时在实现时测量。这些是**每分钟选中的一次观察**：CPU 通常代表该观察前约 30 秒的平均值，内存是瞬时值；不宣称是整分钟均值、连续监控或绝对峰值。

## 5. Admin API 与多执行环境

### 5.1 现有 pool 返回当前值

`GET /admin/agent/runner-hosts` 每个 host 追加服务端 `capacity_epoch` 和可选 `capacity`，后者含上面的安全字段及服务端 `received_at / sampled_at / sample_age_seconds / freshness`；顶层追加 `observed_at` 表示本次 DB 读取时间。epoch 即使没有当前样本也返回，供客户端识别重登记并清掉旧曲线；不在主列展示。原字段与操作路径保留。所有返回 pool 的管理操作共用 `_snapshot()`，也返回同一形状。

只读已存快照，`verify_admin_access` 不变，增加 `Cache-Control: no-store`。单条历史容量 JSON 损坏时返回该 host 容量不可用，不能让整个 Servers 页面 500。

`runner_pool()` 的 policy 和 hosts 目前在默认 READ COMMITTED 事务里读两次。本次改为**只读 repeatable_read**，在同一连接读取 policy、hosts 和统一时间，确保配额与 pin 不混用两个数据库时刻；不更改 claim、reaper、drain、credential 或沙箱状态写入逻辑。

不新建全平台资源总量端点、不增加 DB workload 聚合、不查询事件表或 `api_logs`。`agent_sandboxes` 是登记/协调状态，不能作为实测 inventory 缺失时的无标记替代。

### 5.2 同一物理服务器下如何显示

保留 `labels.physical_host` 的已有展示分组；缺失时用 enrollment host 名称。它不是新的身份或调度键。

1. **CPU/RAM**：在同组成员里选最近的新鲜宿主快照，整组只显示一次，绝不求和或平均百分比。来源不同、boot id 不一致的仍新鲜报告，不自动合成；显示来源不一致并允许展开看各环境。
2. **沙箱**：覆盖组内所有 enrollment，不只读取 `serverInventory()` 选中的代表。相同 namespace 的报告只能取最新一份；不同明确 namespace 的计数才可相加。不同/未知 boot id 不能作为同一 Docker 宿主求和。
3. **不完整覆盖**：旧 runner、过期 namespace、legacy 无归属或身份无法去重时，合计显示 `≥N observed · Partial`，并在展开处列明哪些来源缺失。所有来源都不可用时显示 Unavailable，不能显示 0。无法确定来源是否重叠时不求和，直接显示按环境的计数。
4. **执行配额**：主行延续代表环境的配额，注明对应环境；展开显示其余 enrollment。现有 pin / Drain / Remove 仍使用原 enrollment ID，不发送 physical_host 名称。

同一个物理名称只是配置分组，不能检测所有物理节点或所有 Docker daemon。页面的沙箱总数范围是“本 pool 可识别部署的 Agent 沙箱”，不包含任意其他容器，也不把已 forget、无法归属的 owner 自动补成新 host。首版不提供一个貌似完整的全平台机器总量。

### 5.3 单服务器历史接口

新增 `GET /admin/agent/runner-hosts/{host}/capacity-history?range=1h`，只允许 `1h / 6h / 24h`，默认 `1h`，管理员认证与 no-store 同现有 pool。host 沿用现有 enrollment 标识校验；不存在返回 404，不支持的时间范围/条件返回 422。接口只读 Postgres。

响应包含 `host / capacity_epoch / since / until / observed_at / resolution_seconds=60 / semantics=sampled_observation / points[]`。时间窗由本次 DB 时间锚定；用单个读取视图取得当前 epoch 和对应点，不能先取旧 epoch 再在另一个事务查历史。每点返回 bucket_at、实际 sampled_at、boot id、CPU 利用率和采样窗口、内存已用/总量与利用率、缺测原因。百分比用共享公式生成，CPU/RAM 独立缺测。

按观察分钟输出，最多 1,500 点（覆盖 24 小时及首尾不完整分钟），实际 UTF-8 响应上限 2 MiB。空桶可在读侧补为显式 NULL 点，不存成零值；不返回 carry-forward、插值或平滑曲线，不从已有点推算错过的峰值。NULL、boot id / CPU 数 / 内存总量变化是各相关曲线的分段边界。

主表加载仍只有原 pool 请求；只有展开一台服务器时请求其历史，同一时间只展开一个容量面板。图表固定一个 enrollment 数据来源，默认选打开面板时主行 CPU/RAM 所用的来源；当前缺测时退到代表 enrollment，仍允许查看已存历史。有多个环境时可以切换并标明来源名称。刷新不能偷偷按分钟拼接不同 enrollment 的曲线，也不把同机三个副本画成三台机器。重登记后切到新 epoch 并提示历史从本次登记开始。

## 6. 页面结构与状态

沿用 Admin → Servers，在现有表格补列；可将内容宽度扩大到 `max-w-7xl`，窄屏横向滚动。

```text
Server       Status          Execution slots   CPU     Memory            Sandboxes              Observed    Actions
gpu-worker   online          2/3 · 1 free       37%     33% · 42/128 GiB  3 running · 7 stopped  12s ago     Drain …
                             [environment]            86 GiB available

  Capacity · gpu-worker                         Last 1h [selected]   6h   24h
  CPU usage (%)                                 Memory usage (%)
  [0–100% 折线图，共用时间范围]                    [0–100% 折线图，共用时间范围]
  hover / keyboard: 时间 · CPU 37.2%             时间 · Memory 32.8% · 42 / 128 GiB
  Latest observation: 12s ago · History: one observation per minute
```

- Servers 容量表为主信息；既有 Harness version 与 New chats 选择保留，可放到下方配置区，不改变其行为。
- CPU/RAM 数字只代表最新短窗口/瞬时观察。hover 或小字说明是宿主、采样间隔及数据来源，不引入 `attempt / collector generation` 等主列。
- 展开容量面板显示两张等高的 CPU / 内存利用率折线图；宽屏左右并排，窄屏上下排列，共用时间范围，纵轴固定 0–100%。默认最近 1 小时，可切换 6 小时 / 24 小时；横轴和 tooltip 明确时区，沿用 Admin 的 UTC 时间格式。
- 当前大数字来自最新快照，曲线每分钟一个历史观察点，不能假装拥有每秒实时监控。hover、触摸或键盘定位到实际点后显示其时间和值；内存同时显示百分比与 GiB。曲线使用直线段，缺数据留空，不用平滑曲线造出采样之外的高低值。
- 历史不足时只画已采集到的部分，空态写“历史正在积累”；采集中断处标“无数据”。仍有旧历史但最新观察过期时，保留旧曲线和过期标记，不自动把末尾延长到现在。可以辅助显示“所选范围内观测到的最高值”，不称为绝对峰值。
- 用仓库现有 React / SVG 实现这两个简单折线图，提供可访问的标题、数值摘要与键盘操作；当前 `web/package.json` 没有图表依赖，本期无需引入新库。图表样式沿用现有页面，不建新的监控应用。
- 配额上限、内存可用量和容器数量分别显示。高使用率可用普通进度条；本期不定义告警阈值或“安全/危险”的综合评级。
- 将容量行的 `Last polled` 改为容量 `Observed`；展开展示 heartbeat 时间和所有执行环境。生命周期状态与样本过期可以并存。
- 一行汇合多个来源时，Observed 显示**当前展示组成项中最旧的观察年龄**，展开列出各项时间；新 CPU 样本不能把旧 inventory 冒充为刚采集。客户端在服务端返回年龄上加本地 monotonic 经过时间，不用浏览器墙钟重算，避免调时导致年龄倒退。
- 每 30 秒后台刷新当前 pool 和已展开面板的历史；页面隐藏时暂停，重新可见时立即刷新。每个读取通道至多一个在途请求，全页最多 pool + 一个历史请求；切换账号、撤销权限、卸载时全部 abort 并清空敏感数据。关闭图表面板立即停止历史请求，控制面的历史采样照常进行。
- 同条件网络失败保留带原时间的旧值，显示刷新失败；本地时间继续推进年龄。不能因为轮询失败而让 `12s ago` 永远不变。401/403 清空数据并显示无权访问。
- 操作返回的 pool 不得被更早发出的轮询响应覆盖；用请求代次/取消处理。现有 Drain/Remove 行为及确认语义保留，容量读取不会主动执行这些操作。
- 切换服务器或图表时间范围时，每份历史结果绑定自己的 host、epoch 和 range；旧请求不得配上新标题或新时间轴。重登记引发 epoch 变化时不能把旧曲线混入新结果。历史请求失败只影响该面板，当前值仍可展示；任一请求 401/403 都清空全部管理员数据。
- 首屏用一行/几行紧凑骨架；旧 runner 的容量显示 Unavailable，slots 和已有操作仍可用。加载、空列表、缺测、过期四种情况有各自文案。

## 7. 实现拆分

建议两个可独立验证的实现 PR；本文仍只留在本地。

### PR A：采集、可选心跳、历史与 admin 数据链路

| 文件 | 改动 |
|---|---|
| 新增 `cloud_agent_host/host_capacity.py` | proc 解析、独立 CPU 基线、只读 Docker 清点、有界 collector 与快照缓存 |
| `cloud_agent_host/enrollment.py`、`runner.py` | 启停 collector，心跳可选附带快照，发送时计算 age；不增加生命周期调用 |
| `deploy/host/docker-compose.sandbox.yml` | 仅可信 runner 的三个只读 proc 文件挂载；可禁用采集 |
| 新增纯数据 schema 模块，接入 `cloud_agent/schemas.py` | v1 有界协议、缺测与容错解析；不引入新第三方依赖 |
| 新 migration、`storage/store.py` | 最新快照、时间与 epoch；有界历史表和索引；登记时重置、容量写入核对认证身份、旧报告不续期；一致性读取 |
| 新增容量历史模块，接入 `cloud_agent/app.py` | 独立 60 秒历史 task、按 epoch 去重、24h 过期清理、错误隔离及退出取消 |
| `api/hosts.py`、`api/admin_hosts.py` | 心跳接收、pool 当前值、单 host 历史端点，源 host 来自现有认证，no-store |
| `tests/unit/test_host_enrollment.py`、`test_host_pool_api.py`、`tests/integration/storage/test_host_pool_store.py` 及新 sampler 测试 | 覆盖下面的验收边界 |

### PR B：Servers 展示

更新 `web/src/lib/api/admin.ts`、`admin/hosts/page.tsx`、`format.ts` 与对应测试，新增小型容量历史面板/折线图组件；纯 helper 负责 physical grouping、freshness、namespace 去重、百分比、曲线分段和格式化，不把新逻辑塞进页面的 JSX。先部署 PR A 的控制面再升级 runner，PR B 可以读取旧 runner 缺测；也可先合 UI，让未采集环境显示 Unavailable，旧控制面还没有历史端点时面板显示暂不可用。

## 8. 验收与质量门

| 场景 | 必须成立 |
|---|---|
| 单核持续负载、空闲、多核与 guest accounting fixture | CPU 归一化正确，guest 不加两次，CPU 数不乘两次 |
| 首样本、重启、CPU 热插拔、iowait/总计数回退 | CPU 缺测并重建基线；不输出 0、负数或超 100%；RAM 可正常显示 |
| MemAvailable 缺失/超总量；坏 UTF-8/超长 proc | 正确字段缺测，采集不抛到 liveness |
| 三个 runner 副本、两个 enrollment 共用一台宿主 | RAM 不变三倍，CPU 不相加，相同 namespace 沙箱只算一次 |
| 一个容器内两个 Fork 成员；同 group 意外两个容器 | 前者计 1 个实体，后者计 2 个并提示异常 |
| stopped、paused、restarting、foreign namespace、无 namespace | 按既定状态/归属显示，不把 paused 当已释放，不把未归属当不存在 |
| 0 个容器 vs Docker 超时/失败/超过4096行或1MiB | 只有成功完整空清点为 0；其余缺测/部分，子进程和输出确实受界限约束 |
| observer 权限缺失、被关闭；旧 runner 不报 capacity | 心跳照常、原 claim 行为不变，容量缺测或自然过期 |
| 连续 liveness、重复 sample_id、重试旧快照 | 同一旧缓存上报到第91秒，穿插另一副本上报，仍不得被续成新鲜；更老观察不能覆盖较新样本 |
| 坏容量字段/未知 schema；重注册；坏历史 JSON | 不阻断合法心跳或整个 pool；PG 并发复现旧凭证先鉴权、新登记清空后再迟到写入，容量更新必须被拒绝 |
| pool 读取中同时发生 pin/claim | 同一个 repeatable-read 视图；不能靠先取 ID 再二次事务冒充一致性 |
| 旧轮询在 Drain 后返回、401/403、账号切换、隐藏标签页 | 旧响应不能覆盖新状态；权限撤销立即清空；恢复可见立即刷新 |
| 多源观察年龄不同、浏览器墙钟跳变 | 主行用最旧组成项时间，各来源仍可展开；时效按本地 monotonic 前进 |
| 3 个 runner 副本、多历史 task、重复 sample_id | 每个 epoch 每分钟最多一个观察；同一样本不能在下一分钟变成新点 |
| 旧 epoch 快照被读取后重登记，后台任务迟到写入 | 新图只读当前 epoch；不能给旧样本盖新 epoch；Remove 保留历史，Forget 按现有硬删语义级联 |
| 关闭浏览器后采集，再重新打开；只有 10 分钟历史 | 历史持续积累且可恢复；不足的窗口留空，不虚构过去 24 小时 |
| 缺桶、CPU 独立缺测、宿主重启、内存容量变化 | 对应曲线断开；不补 0、不跨身份/硬件边界连线，缺 CPU 不隐藏 RAM |
| 历史 DB 写入/清理超时、服务停机后恢复 | 心跳和 reaper 照常；清理每轮有界，查询不越过 24h，不补造历史 |
| 展开/关闭、切服务器、1h→24h、旧请求迟到、401/403 | 请求数有界，曲线与来源/窗口始终匹配，权限失效清空全部数据 |

按 Agent 仓库要求执行 Python check、相关真实 PG 测试，以及 Web prettier/eslint/tsc/vitest/production build。为两个 CPU 周期、30/90 秒阈值和异步轮询使用可控时钟，不写真实 sleep 的脆弱测试。

性能验收使用合成 100 个 pool host、1,000 个 Docker 清点行及约 144,000 行历史；记录实际大小、耗时与查询数。容量块 ≤8 KiB、pool 增量线性；历史按 host/epoch/时间索引读取，单次 ≤1,500 点 / 2 MiB；主表不预载每台机器的曲线。DB 读取不随事件数/任务数增长；不存在逐容器 RPC 或逐 host 网络调用。没有测量前不声称延迟已达标。

staging 验收：按现有 host 发布流程逐步升级，保留真实会话；使用受控只读/轻量任务观察 slots 上升再下降、CPU/RAM 随负载变化；任务完成后保留容器数可以不下降。另测一个 Fork，确认容器数不翻倍。至少积累 10 分钟真实历史，关闭浏览器后重新打开核对曲线仍在；切换 1h/6h/24h，已采集片段一致、空白时段无虚构数据。使用测试替身/隔离测试实例模拟 Docker 卡住、collector 失效和过期，不为验收停止共享 broker 或删除保留容器。生产不动。

交付报告记录实际 deployed SHA、迁移、runner 副本是否已换版、物理分组覆盖、浏览器截图与缺测原因。合并不等于所有 runner 已采集成功；不以 CI 替代这一步。

## 9. 后续接口边界

本期 CPU/RAM 是宿主压力，sandbox inventory 是实体数量，slots 是调度配额。24h 历史表达短期变化，不能代替长期趋势或整轮资源归因。后续“整轮资源统计”仍需独立解决共享沙箱的成员归因；“逐次 LLM 请求”仍接网关请求记录；告警可以消费本期时效明确的采样，但必须自行定义持续时间与缺测规则。

## 10. 参考 hybridInference Admin 后的产品建议

参考基线：本地 gateway `96e2e1c29141243a9819b0bafd295a097936d936` 的实现及设计稿，未据此声称线上页面已经部署了该版本。本节区分本期展示建议和后续候选；后续候选不计入 §7 两个容量 PR 的验收范围。

### 10.1 已有设计中值得沿用的部分

| gateway 参考 | 借鉴到 Agent 的方式 |
|---|---|
| [Provider Performance compact design](https://github.com/HarvardMadSys/hybridInference/blob/96e2e1c29141243a9819b0bafd295a097936d936/docs/superpowers/specs/2026-05-09-provider-performance-compact-design.md) | 摘要用紧凑的一行，表格承担横向比较，图表保持足够高度；避免大卡片和装饰挤走内容 |
| [RequestPerformancePanel](https://github.com/HarvardMadSys/hybridInference/blob/96e2e1c29141243a9819b0bafd295a097936d936/apps/frontend/src/components/features/admin/RequestPerformancePanel.tsx) | 先比较各行指标，选中一行才加载趋势；展开失败不应破坏其余列表 |
| [EndpointTrendCharts](https://github.com/HarvardMadSys/hybridInference/blob/96e2e1c29141243a9819b0bafd295a097936d936/apps/frontend/src/components/features/admin/EndpointTrendCharts.tsx) | 单位明确、图例有文字、显示有效采样范围、缺样断线、孤立的单点仍可见；CPU/RAM 各自成图 |
| [Users SummaryCards](https://github.com/HarvardMadSys/hybridInference/blob/96e2e1c29141243a9819b0bafd295a097936d936/apps/frontend/src/app/dashboard/admin/users/SummaryCards.tsx) / [SavedViews](https://github.com/HarvardMadSys/hybridInference/blob/96e2e1c29141243a9819b0bafd295a097936d936/apps/frontend/src/app/dashboard/admin/users/SavedViews.tsx) | 摘要数字点下去能筛出对应列表；将反复使用的筛选保存为视图 |
| [AnalyticsCards](https://github.com/HarvardMadSys/hybridInference/blob/96e2e1c29141243a9819b0bafd295a097936d936/apps/frontend/src/app/dashboard/admin/AnalyticsCards.tsx) | 以活跃用户、使用趋势、模型分布和主要使用者回答平台运营问题，适合补在 Agent Usage 上 |

借鉴交互与信息层次，不直接复制请求时延的统计口径。gateway 目前使用 Recharts；Agent 基线没有这项依赖，容量图仍可按 §6 用现有 React/SVG 实现。gateway 的平滑曲线、日期 locale 和某些独立时间窗也不直接沿用，Agent 保持本文约定的直线分段、明确 UTC 与来源/窗口绑定。

### 10.2 本期可一起完成的展示增强

- **可排序、可筛选的服务器表**：按新鲜 CPU 利用率、内存利用率排序；快速筛选在线、draining、offline 或容量已过期。缺测/过期值始终与新鲜值区分，不能当 0 排成“最空闲”。这些都基于 pool 已返回的数据，不新增“安全容量”推断或告警阈值。
- **曲线显示覆盖情况**：例如“CPU 58/60 分钟有观察，内存 60/60”。分母取窗口内预期的分钟桶，CPU/RAM 分开计，首尾不完整分钟明确标记；部署不足一小时导致的空白是“历史不足”。缺桶断线，只有一个有效点时也画出圆点。
- **当前值、来源、时效紧凑排列**：让数字和趋势占主要空间；较长的环境标识/技术解释放在展开区。延续 §6 的按需加载，不给每台服务器预加载整段历史。

### 10.3 后续候选，按产品价值排序

| 顺序 | 管理员的问题 | 建议功能与位置 | 需要补的基础 |
|---|---|---|---|
| 1 | 现在有多少任务在等，最久等了多久？ | Runs 顶部加运行概况：正在执行、可调度队列、等待前序任务、最老等待；点击数字进入同一集合 | 新的全量聚合查询；不能把当前 50 行分页求和，也不能让默认 7 天窗口漏掉仍在等待的旧任务 |
| 2 | 最近是不是更慢、更容易失败了？ | Runs 加按日的成功/失败/取消数量，以及提交到首次领取的中位数/P95；按 runtime、model、host 分组比较 | 明确 cohort、结果状态和时间定义，再做跨任务聚合；取消单列，成功率不把未结束任务当失败 |
| 3 | 负载变化是不是和并发量一起发生？ | 服务器容量面板增加同一时间轴的执行配额占用曲线，分别比较 CPU、内存、并发执行数 | 历史 task 同步采样该 enrollment 的 slots_used/total，记录自己的观察时间；队列总量属于平台，不能任意归给某台 host |
| 4 | 最近谁在用，费用为什么涨了？ | Usage 增加每日费用/token 曲线、活跃 Agent 用户、runtime/model 使用分布、主要使用者；点击筛到 Runs | 复用 #168 的账本与覆盖率口径；活跃用户定义为窗口内提交 Agent 任务的去重用户，不拿网关登录人数代替 |
| 5 | 怎样少做重复筛选、把发现交给同事？ | Runs/Usage/Servers 的固定筛选视图与可分享链接，后续再做表格导出 | 保存完整筛选；分享固定时间窗时不能被接收方的“现在”悄悄移动；各页只接受已支持的参数 |

排队统计需要特别谨慎：Agent 目前有 `waiting`（等前序任务）和 `queued`（可以参与调度）两种状态，不能合成“机器不足导致的排队”。现有 `job.created_at → first attempt.started_at` 可以叫“提交到首次领取”，会包含依赖等待；要精确叫“调度排队时长”还需记录进入 queued 的时点，不能用 updated_at 猜。

建议执行次序：先完成本期 Servers 当前值与曲线；紧接着做运行概况和等待统计，再补任务结果趋势。Usage 的图形展示可作为独立增强；逐次 LLM 请求与整轮资源归因继续沿原路线推进。首批运行概况放在 Runs 页，不为少量新指标再建一个空泛的 Admin 首页。

本节不恢复前面已经移出范围的单次执行排障流程。账号/Provider/路由管理继续属于 gateway；Agent 侧集中回答任务、用量和执行容量。告警在有稳定历史与缺测规则之后增加，按持续时间与恢复状态处理，避免一次短暂峰值就通知。
