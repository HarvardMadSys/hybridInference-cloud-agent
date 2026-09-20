# e77e3e6 文件与终端服务端测量 review

日期：2026-09-16。对象：本地提交 `e77e3e67f807770135968b7755b7782f87db6c7f`，
parent `3a191e5`。本轮仅 review 与设计，**未修改实现、未 rebase、未 push**。

结论：服务端接入方向可继续，但下面五项需要修复。把这份 review 与
[存储/Admin 整合设计](2026-09-16-operation-measurement-storage-admin-integration-design.zh-CN.md)
一起作为下一轮实现输入；不是先把当前记录原样存入数据库。

## R1 · P2：并发 continuation 共用可变对象，丢请求还报告完整

位置：[operation_measurement.py:426](../../cloud_agent/operation_measurement.py#L426)
及 [routes.py:2943](../../cloud_agent/api/routes.py#L2943)。

首个请求结束后，两个重叠请求携带相同续接 ID：第一个建立新 Selection，第二个
进入 `_continue()` 的 unfinished 分支并获得**同一个对象**。两个 begin_call 都得到
c1，后一次调用没有登记新的期望；先结束的请求 finish 整个 measurement，后结束的
应答被忽略。实际复现：

```text
same_mutable_selection = true
call_ids = ["c1", "c1"]
expected_fragments = [["broker", "c1", "create"]]
second_answer_accepted = false
last_record_complete = true
```

两个本应独立记录/计费的请求变成一个样本，足以影响请求覆盖、预算和性能分布。
另外，即使串行，各请求仍复用 measurement_id，不能直接拿来做数据库幂等键。

修复：interaction 只关联，measurement 每个请求独有；选择缓存不共享可变 collector。
每次物理调用显式铸造 call ID，应答使用该调用捕获的 ID，不通过可变的“当前 call”
猜测。增加两个请求重叠、逆序返回、一条失败另一条成功的路由测试；断言两条记录、
独立名额、各自片段及完整性。

## R2 · P2：续接绕过当前目标绑定，沿用旧 host/sandbox

位置：[operation_measurement.py:366](../../cloud_agent/operation_measurement.py#L366)
及 [operation_measurement.py:432](../../cloud_agent/operation_measurement.py#L432)。

observe 收到本次 Root，却只将 operation/job_id 传给 `_continue()`；重启一条记录时
再从旧 selection 拷贝 facts。若同一 job 的当前 host/sandbox 解析结果变化或变成未知，
原来 scope=host-a 的选择仍被接受，并继续把新请求标为 host-a。复现：

```text
run.target.host = "host-a"
first Root.host = "host-a"
continued Root.host = "host-b"
selected_on_wrong_host = true
recorded_host = "host-a"
```

这不是业务授权绕过，原有 owner 检查仍在；问题是**实验范围和测量归属失真**。
修复：每次续接用当前认证与资源解析结果核对范围和交互绑定；变化/缺失时不采集，
不使用旧 facts 填空，不重新抽样来伪装续接。增加 host 变化、sandbox 变化和事实
缺失的测试；同时验证无测量仍正常完成业务。

## R3 · P2：off 响应没有保持原契约

位置：[routes.py:2902](../../cloud_agent/api/routes.py#L2902)，并检查共享
AgentWorkspaceResponse/AgentTerminalSessionResponse 的其他路由。

`model_dump(exclude_none=True)` 去掉所有 null，并不只去掉新增 profiling。
使用 parent `3a191e5` 的真实响应模型与新 helper 分别挂到 FastAPI 测试路由，
同一目录 payload、profiling 关闭时：

```text
removed_root_keys  = ["content", "omitted_reason", "size", "status"]
removed_entry_keys = ["omitted_reason", "size", "status"]
```

现有 off 测试把已经省略这些字段的新形状写成期望，因此测试通过不能证明与 parent
兼容。反方向上，共享响应模型新增 profiling 后，未改为 helper 的 PUT files 及
terminal input/resize/delete 等路由会默认序列化 profiling:null。

修复：只省略未启用的新增 metadata，保留原字段和嵌套 null 语义；避免把根操作
metadata 放入通用 terminal item 后泄漏到未接入路由。用 parent 的实际 HTTP 形状
做回归基线，覆盖文件读写、目录 entries、终端 list/create/input/resize/delete。

## R4 · P2：公共响应丢掉 broker 截断状态

位置：[routes.py:2872](../../cloud_agent/api/routes.py#L2872)。

record_answer 正确把 profiling_truncated 留在 measurement 内；但
`_profiling_fragment()` 只传 recorded，没有传 truncated，metadata 默认 false。
SSE 响应头也复用这个 helper。broker 已应答但过期截断时，实际得到：

```text
public fragment: recorded=true, truncated=false
stored/log record: truncated=true, measurement_complete=false
```

下一步浏览器或 Admin 聚合会从公共响应得到与根记录相反的完整性信号。
修复：明确 fragment 与根记录的完整性字段，保留 broker 截断/过期跳过，并在响应
构建前完成根的截止判定。测试 JSON 和 SSE 两条路径，包含空 stages + truncated、
有 stages + truncated、正常空应答，以及 CP 自身期限先到的情况。

## R5 · P2：子目录被当作 files.open，files.list 实验漏测展开

位置：[routes.py:2979](../../cloud_agent/api/routes.py#L2979) 与
[workspace_broker.py:1926](../../apps/backend/cloud_agent_host/workspace_broker.py#L1926)。

CP 和 broker 都按 path 是否为空分类。配置只选 `files.list`，依次请求根目录与
`path=src`，让 broker 对两者都返回 kind=directory，真实 CP HTTP 路由复现：

```text
statuses = [200, 200]
kinds = ["directory", "directory"]
root_selected = true
subdirectory_selected = false
record_count = 1
```

配置 files.open 时反而会把展开目录混入文件打开分布，与设计的“目录列表/文件
预览”契约不符。添加 facts.kind 不能补救被前置 operation filter 丢掉的请求。
修复：通过原请求结果或已验证的操作类型确定类别；不新增探测 exec。整合设计 §3.3
给出了有界候选与后置定类方案。增加只选 list、只选 open、两者都选时的根目录、
子目录、文件和定类前错误场景。

## 验证范围与未交付能力

reviewer 实际运行：

```text
python -m pytest -q tests/servers/test_operation_measurement_routes.py \
  tests/unit/test_agent_operation_profiling.py
89 passed in 1.27s
```

此外分别执行了上述五项定向复现：R1/R2/R4 调用真实选择器和响应 helper；R3 用
parent 版本响应模型经过 FastAPI 序列化作对照；R5 走真实 CP HTTP 路由、broker
使用返回子目录的测试替身。没有把现有 89 个通过测试说成五项问题已修复。

本轮没有重跑全量 make check、真实 Docker/Kata、浏览器或数据库测试。作者报告的
3261 passed 与本次 review 证据分开看。浏览器终点、跨 replica 配额、CP 持久化及
Admin 尚未交付，属于明确的后续范围，不作为本次新增缺陷重复列出。
