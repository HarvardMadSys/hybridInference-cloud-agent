# 3e7af8e 服务端修复与持久化基础 review

日期：2026-09-17。检查 head：`3e7af8e`；涵盖 `b9d5e8a`、`5d12d40`、
`8f48f3d`、`32a6076`、`3e7af8e`。本轮只 review，没有修改实现或 push。

结论：每请求独立 measurement、保留业务 null、按结果分类子目录等修复已进入代码，
但不能把上一轮五项问题全部视为闭合。下列问题需与后续 endpoint/exporter/Admin
一起修复。它们是当前代码的可复现问题；已明确尚未实现的写入入口、后台投递和页面
不重复作为缺陷列出。总体契约仍见
[存储/Admin 整合设计](../specs/2026-09-16-operation-measurement-storage-admin-integration-design.zh-CN.md)。

## 1. P2：文件读取的真实阶段耗时丢失

位置：[routes.py:3154](../../cloud_agent/api/routes.py#L3154)。

`_measured_call(..., stage="read")` 包围真实 RPC，时间挂在 read 下。但响应回来后才
`mark(files.open/files.list)`，并用一个空的 `with ...: pass` 重新生成 sandbox_manager_call。
最终 `_stages()` 只输出后打的 mark；先前 read 的真实本地计时因此消失。

真实 CP HTTP 路由配合耗时约 50 ms 的 sandbox manager 替身，输出：

```text
duration_us = 50647
collection_complete = true
stage files.open: start_offset_us=50563, duration_us=84
  control-plane sandbox_manager_call: duration_us=1
  sandbox manager exec: duration_us=50484
```

总时长仍在，但“时间花在哪里”这项核心功能错误：父阶段仅 84 µs，却挂了约 50 ms
的 sandbox manager exec，CP 的 RPC 包围区间反而显示 1 µs。

修复建议：候选操作进入时就 mark 固定的 read 阶段，真实 RPC 与应答都挂在该阶段。
读取结果只用于确定根 operation，不必重新命名时间轴；删除读后空 span。若要改名，
必须保留原起点和完整 children。测试注入可控延迟，断言 RPC 区间包含延迟且 read
从调用前开始，不能只断言 children 的名字存在。

## 2. P2：continuation 仍会重抽样，且未保存原交互范围

位置：[operation_measurement.py:561](../../cloud_agent/operation_measurement.py#L561)
与 [operation_measurement.py:602](../../cloud_agent/operation_measurement.py#L602)。

`observe()` 在 `_resume()` 失败后仍调用 start。缓存淘汰、引用失效或另一 replica
找不到该交互时，会采一个新 interaction，继续消费预算。整合设计要求此时继续业务、
停止这次续接采集，不能以新样本伪装连续测量。

同时 Interaction 没有保存最初解析出的 host/thread/sandbox facts，`_resume()`
只检查 run.target 指定的字段。实验未显式 target host 时，即使原交互的 host 和
sandbox 都改变，仍复用同一个 interaction。复现：

```text
target = {}
first request: host-a / sandbox-a
next request:  host-b / sandbox-b
measured = true; same_interaction = true

continuing = "msm_evicted_reference"
measured = true; fresh_interaction = <new id>
```

另一个同源错误：run.target.job_id 存在时，`_resume()` 从 scope.facts 中查 job_id，
但它实际在 scope.job_id。合法的相同 job 续接被拒，然后 fallback 新建交互。

```text
target = {job_id: "j1"}; both requests belong to j1
same_interaction = false
```

修复建议：区分“无续接引用”和“给了引用但验证失败”，后者直接不采集；选择表保存
已验证的不可变归属 facts，每次与当前解析结果核对，即使 run 未按该字段过滤也要
保持同一交互身份。目标校验使用包含 scope.job_id 的统一 facts。补齐无 host target
时资源变化、失效/淘汰/跨实例引用、job_id target 的真实路由测试；普通未带引用的
新请求仍可正常采样。

## 3. P2：响应完整性与最终记录仍有截止点竞态

位置：[operation_profiling.py:1330](../../apps/backend/cloud_agent_host/operation_profiling.py#L1330)
与 [routes.py:2953](../../cloud_agent/api/routes.py#L2953)。

settle 只检查当时是否过期，没有冻结结束边界或最终完整性。响应生成 metadata 后，
JSON 编码/响应构建仍可跨过截止点，随后 finish 再检查时钟，改成 partial。
因此上一轮的 sandbox manager 截断透传已修，但公共响应与记录的一致性还未完全解决。

可控时钟复现，所有 sandbox manager 片段均正常应答：

```text
deadline = 101
render metadata at 100: collection_complete=true
finish at 102:          collection_complete=false
```

修复建议：在一个明确、版本化的边界完成一次 finalize，冻结 record/metadata 共用的
时间和完整性；后续 export 只投递，不重新解释边界。如果选择在序列化前冻结，就如实
定义终点不包含最后的响应编码，不能又把后续时间算进同一记录。对 JSON 与 SSE
分别测试“采集结束前未到期、响应构建期间到期”，并要求两者与导出记录一致。

## 4. P2：统计混入 partial/失败，也默认混合启动路径

位置：[operation_measurements.py:294](../../cloud_agent/storage/repositories/operation_measurements.py#L294)
与 [operation_measurements.py:317](../../cloud_agent/storage/repositories/operation_measurements.py#L317)。

SQL 虽单独统计 incomplete_count/failed_count，但 percentile/min/max 没有使用
相同的成功完整样本过滤；这些样本仍进入主分布。真实 Postgres 插入：

| 样本 | duration_us | outcome | collection_complete |
| --- | --- | --- | --- |
| a | 100 | completed | true |
| b | 10000 | failed | true |
| c | 20000 | completed | false |

当前 p50=10000、p95=19000；按设计，成功完整主分布只能使用 a，应均为 100。
保留失败/partial 的数量不等于把它们排除出了分位数。

另外，默认分组没有 startup_path，运行时版本/类别也不能作为当前分组维度。
`new_group=1000000 µs` 和 `reuse_live=1000 µs` 实际合并成一组，p50=500500。
调用者传 group_by 还可去掉 boundary_version/coverage_scope；非法字段被静默丢弃，
不能依赖调用者始终记住不可混合的边界。

修复建议：主统计的 p50/p95/min/max 使用 `collection_complete AND
outcome IN ('completed','dispatched')` 的 FILTER，单独返回 eligible_count 和全部
样本的失败/缺失数；没有 eligible 样本时分位数为 null。operation/request_kind/
boundary_version/coverage_scope 为不可移除的口径分组，startup 再按 path/runtime
区分。若保留跨路径总览，单独命名为混合概览，不能冒充优化比较的默认分布。

增加实际数值测试：失败和 partial 的耗时与成功值明显不同、全 partial、两条启动
路径、不同 runtime，以及调用方试图减少必需维度的情况。

## 5. P2：同 run_id 的冲突 manifest 被忽略，新样本仍入库

位置：[operation_measurements.py:64](../../cloud_agent/storage/repositories/operation_measurements.py#L64)。

run INSERT 使用 ON CONFLICT DO NOTHING，没有校验已有的 operations/target/expiry。
record ID 冲突已处理，但实验身份冲突没有处理：同一个 run_id 先按 host-a 注册，
再带 host-b 的 manifest 写入另一 measurement，返回 stored，目录却仍声称只选 host-a。

真实 Postgres 复现：

```text
second write result = "stored"
stored run.target = {host: "host-a"}
incoming run.target = {host: "host-b"}
new sample exists = true
stored admission_instances = [cp-1]  # incoming cp-2 did not enter the manifest
```

后续 run catalogue/endpoint 的接线不能绕开这个 repository 层的原子一致性。
修复建议：同一事务中确认 run 的不可变配置相同；不同则明确返回不可重试的 run
冲突，且不插入 measurement。相同实验的新 admission instance 应有界、幂等地合并
实例描述；同实例预算发生冲突也要有明确结果，不能静默保留第一份。增加两个并发
writer 争用 run_id 的真实 DB 用例；仅在 handler 中先查再写不足以覆盖竞争窗口。

## 本轮验证与进度判断

实际运行相关测试：

```text
python -m pytest -q tests/servers/test_operation_measurement_routes.py \
  tests/unit/test_agent_operation_profiling.py
111 passed in 1.88s
```

额外定向复现：真实 CP 路由的慢文件读取；真实选择器的三个续接场景；可控时钟的
metadata/finish 跨截止点；真实 Postgres 的分布、路径混算、run manifest 冲突。
数据库复现全部放在一个事务中并回滚，未清空原测试库或删除数据库。

没有重跑作者报告的 4103 项全量测试与 10 项原 dbtest，也没有把它们的通过当成本轮
问题已被覆盖。现有分页测试还只让 observed_at 相同，而分页键是 received_at；补验
相同接收时间时，应明确使 received_at 相同，再检查 ID tiebreaker。SQL 的 keyset
写法本身没有在本次发现错误。

当前可准确描述为：**采集器/路由修复与持久化基础已提交，仍有上述正确性问题；运行时
持久化、鉴权/校验/队列、清理和 Admin 尚未贯通。** 不要求等待额外确认才能继续既定
范围；先把这些问题带入实现，再完成采集 → 持久化 → 页面的一次端到端验收。
