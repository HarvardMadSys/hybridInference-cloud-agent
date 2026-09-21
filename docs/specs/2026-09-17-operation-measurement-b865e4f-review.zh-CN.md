# b865e4f 复核：run manifest 的期限与首次并发创建

日期：2026-09-17。基线：`b865e4f6babace0442da1135683e3a99cface667`。
本轮只 review，未修改实现或 push。

前一轮的固定 read 阶段、失效续接不重新抽样、当前归属核对、finalize 截止判定、
成功完整样本过滤与启动路径分组已进入代码。本轮仍有两项 P2 问题需要先修，
两者都在真实 Postgres 上复现。

## 1. P2：不能忽略实验配置中的绝对 expires_at

位置：[operation_measurements.py:398](../../cloud_agent/storage/repositories/operation_measurements.py#L398)。

当前 run 冲突比较完全忽略 expires_at，既没有允许误差的上限，也不拒绝不同实验
期限。同 run_id 的第二个 writer 把期限延后一天，仍返回 stored，目录保留旧时间：

```text
write_result = "stored"
accepted_expiry_delta_seconds = 86400
catalogue_expiry_unchanged = true
```

这会把不同有效期的实验样本归到同一个 manifest，也使后续 writer 的时间范围与
Admin 目录或 ingest 使用的截止时间不一致。

“相同配置每个进程重新推导，有微秒漂移”混淆了两种值：

- 原始实验配置的绝对截止：`ProfilingRun.expires_at` 直接保存 descriptor 的值，见
  [operation_profiling.py:492](../../apps/backend/cloud_agent_host/operation_profiling.py#L492)。相同配置
  表示同一个绝对时间，规范化为 UTC datetime 后可以精确比较。
- 当前 RPC 继承的有效截止：measurement_context 从单调期限换算回墙钟，见
  [operation_profiling.py:933](../../apps/backend/cloud_agent_host/operation_profiling.py#L933)。这个值
  可以有换算误差，并可能被 producer 缩短；不应用它重新定义 run manifest。

修复建议：run manifest 一直携带配置中的原始绝对时间，比较规范化后的 UTC instant。
不同期限返回 run_conflict；需要换实验期限就使用新 run_id。RPC 的有效截止另存或
仅用于下游限时，不作为实验身份。原样队列重试保留同一个值，反而应通过相等校验。

测试 fixture 的 `_run()` 每调用一次就重新执行 now()+30min，这制造的是不同的输入
配置，应冻结一个实验时间再用于重试；不能通过放宽业务规则来迁就该 fixture。
现有“微秒漂移”测试实际传 now()+5h，并断言 stored，应改为冲突测试。

验收：相同绝对时间的不同 ISO 时区表示视为相同；相同 run 的原样重试成功；期限
延长或缩短都拒绝；RPC 有效期限缩短不修改 catalogue 的原始期限。

## 2. P2：FOR UPDATE 锁不住尚不存在的 run 行

位置：[operation_measurements.py:70](../../cloud_agent/storage/repositories/operation_measurements.py#L70)
及 [operation_measurements.py:100](../../cloud_agent/storage/repositories/operation_measurements.py#L100)。

两个 CP/runner writer 同时首次上报一个 run 时，两个 SELECT FOR UPDATE 都可以
返回空结果。后面的普通 INSERT 会竞争主键，一方抛 UniqueViolationError，无法按
设计返回 stored/duplicate/run_conflict；即使两份 manifest 完全相同也发生。

使用两个真实 Postgres 连接，在双方 SELECT 空结果后同步放行 INSERT，稳定复现：

```text
writer A: stored
writer B: UniqueViolationError
          duplicate key value violates unique constraint
          "agent_operation_profiling_runs_pkey"
```

这正是多个 producer 开启实验时的正常场景，不能靠顺序执行的 admission merge 测试
证明无竞争。后续 exporter 不应把本可幂等处理的首次写入竞争当成未知存储错误。

修复建议：事务内先 INSERT ... ON CONFLICT DO NOTHING 建立行，再 SELECT FOR UPDATE
读取并比较最终的 run 配置，最后合并 admission 和插入 measurement。冲突检查仍需
执行，不能因为 INSERT 没插入就直接接受。也可采用等价的按 run_id 串行化方案，
但必须覆盖“行尚不存在”。

验收：两个兼容 writer 首次并发均成功且各自样本保留；两份不兼容 manifest 并发
只有一份被接受，另一份返回 run_conflict 且其 measurement 不存在；相同 measurement
并发重传仍只保留一条。对这些正常竞争均不得向调用者泄漏 UniqueViolationError。

## 实测范围与下一步

```text
python -m pytest -q tests/servers/test_operation_measurement_routes.py \
  tests/unit/test_agent_operation_profiling.py
119 passed in 2.14s
```

另执行上述两项真实 Postgres 定向复现；仅清理本轮 UUID 标识的临时 review 行，
未删除库或清空已有测试数据。没有重跑作者报告的全量 4111 项与原 18 项 dbtest。

修复两项后继续既定的 worker endpoint、有界后台投递、run catalogue、Admin API/UI
及端到端验收，不需要重新确认这些既定范围。原始设计仍见
[整合设计](2026-09-16-operation-measurement-storage-admin-integration-design.zh-CN.md)。
