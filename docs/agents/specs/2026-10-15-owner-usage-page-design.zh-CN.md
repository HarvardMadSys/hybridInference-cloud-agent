# 设置：用量（Usage）

日期：2026-10-15。状态：与实现一致（#677，以及标注估算和 UTC 零点的后续 PR）。

目标：让用户在设置里看到自己有几个容器、现在用了多少资源，以及过去 7 天或 30 天里自己的工作用了多少。

## 一 已确定的产品决定

| 问题 | 决定 |
| --- | --- |
| 放在哪里 | 设置左侧导航里的“用量”一项，页面 `/settings/usage`，分“容器”和“历史用量”两张卡片。 |
| 要不要“停止容器”按钮 | 不要。容器空闲 30 分钟会自动停止；停止也不释放磁盘，释放磁盘靠“永久删除对话”。页面只读。 |
| 模型用量显示美元吗 | 显示。数字与管理员 Usage 页相同，只是只算自己的轮次。标为“估算，不是账单”，与 gateway Dashboard 的说法一致。 |
| 对用户怎么称呼 | “容器”（Containers）。不用“工作环境”，避免和已有的 Environments 混淆。 |
| 日期 | 按 UTC 自然日，不按用户本地时区：gateway 的每日额度在 UTC 零点重置，两边的“今天”要一致。页面写明这一点，并显示 UTC 零点在用户时钟上是几点（北京时间 8:00）。一轮对话计入它创建的那一天；CPU 和内存计入实际使用的那一天。 |

## 二 页面内容

1. **容器（当前）**
   - 汇总：总数和各状态个数；运行中容器当前合计的 CPU 和内存（只算 90 秒内的新鲜读数，其余单独说明个数）。
   - 列表：运行中的在前，然后按最后使用时间倒序，每页 50 个，可以“再显示”。每个容器显示所属对话（可点开，Fork 共用的容器列出多个对话，已归档的对话标“已归档”）、仓库、状态、CPU 和内存用量及上限、空闲多久后自动停止或最后使用时间。
   - 状态映射：`provisioning`/`starting`/`restoring` → 启动中；`running` → 运行中；`stopping` → 停止中；`stopped`/`stopped_dirty` → 已停止；`cold` → 已存入存储；`lost` → 不可用；删除进行中（`deletion_state` 为 `pending` 或 `archived`）→ 删除中。
   - 不显示：删除已完成的容器；所有对话都被用户永久删除的容器。
2. **历史用量（7 天 / 30 天）**
   - 模型费用、Token（输入/输出）、容器运行时长、CPU 核·小时、内存 GB·小时。
   - 每天一根柱子，可切换上面五个指标；按模型的费用、Token 和轮次。
   - 每日额度在 gateway，本服务读不到，放链接到 Dashboard。

## 三 数据来源

| 指标 | 来源 | 历史从什么时候开始 |
| --- | --- | --- |
| 当前 CPU、内存 | `agent_sandboxes.resource_snapshot`：host 每 15 秒采一次 Docker stats，随心跳（30 秒）上报，只保留最新一条 | — |
| 运行时长 | `agent_sandbox_events`：数据库触发器记录每次状态变化；`starting`、`running`、`stopping` 计为运行 | `0046` 迁移上线时 |
| CPU 核·小时、内存 GB·小时 | 新表 `agent_sandbox_usage_days`，见下节 | 本 PR 的 `0077` 迁移上线时 |
| 费用、Token | 管理员 Usage 页的同一套汇总（`AdminUsageQueries.selection`），`user_id` 固定为当前用户 | 已有 |

## 四 CPU 和内存的累计

控制面每接受一条新的采样（`sample_id` 不同且时间更新），就把它代表的时长和这段时间的用量加到该容器、该 UTC 日的一行里：

- 代表的时长 = 距上一条被接受的采样的间隔（最多 120 秒）。因为 host 每 15 秒采样、每 30 秒上报，控制面大约每隔一条才看到一条，所以必须用间隔，不能只用采样自己的 15 秒窗口。
- 容器刚启动（没有上一条）或间隔超过 120 秒（丢了上报）时，只算这条采样自己的 CPU 窗口，不假设中间一直在用。
- CPU 核·秒 += CPU 核数 × 时长；内存字节·秒 += 内存字节 × 时长；同时累加“已测量时长”。CPU 或内存缺失的采样不计。
- 写入放在单独的 savepoint 里，失败只记日志，不影响管理员页面看到的最新采样。

页面会对比“已测量时长”和“运行时长”，相差超过 10% 时说明“CPU 和内存只覆盖了部分运行时长”。

## 五 接口

都只返回当前用户自己的数据，带 `Cache-Control: no-store`，不包含 host、container ID、镜像或 generation。

- `GET /v1/agent/usage/containers?cursor=&limit=`（默认 50，最多 100）
- `GET /v1/agent/usage/resources?days=`（1–90）
- `GET /v1/agent/usage/tokens?days=`（1–90）；选择范围太大返回 422，读取失败返回 503，都不返回部分合计。

## 六 存储

迁移 `0077_sandbox_usage_days`：

- 新表 `agent_sandbox_usage_days (sandbox_id, user_id, day, measured_seconds, cpu_core_seconds, memory_byte_seconds, updated_at)`，主键 `(sandbox_id, day)`，外键 `(sandbox_id, user_id)` 指向 `agent_sandboxes`，按 `(user_id, day)` 建索引。
- `agent_sandboxes (user_id)` 新索引：以前没有按用户读容器的路径。

## 七 不做的事

- 不测量磁盘实际用量（只有上限，老容器没有上限）。
- 不回填 CPU 和内存历史：旧的采样已经被覆盖。
- 不做配额或限制；这些数据以后可以作为配额的基础。
