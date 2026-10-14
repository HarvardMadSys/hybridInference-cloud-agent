# Admin 测量：开启后持续采集，直到 Disable

日期：2026-09-22。用户确认：不选择 duration，开启后一直测，直到点击 Disable。
本设计接续 #318 / #320。以下行为需本次代码发布后生效，不代表已经部署。

## 页面

- 默认 Disabled。Enable 后选择测量对象与 host，提交一次即可；不选持续时间。
- Enabled 时显示对象、host、样本数和“持续采集，直到 Disable”，提供 Disable 按钮。
- 删除控制卡里的 Details：mode、revision、预算等内部字段仍可通过 Admin API 排查。
- host 下拉只列出已注册且 online / draining 的 runner；All hosts 仍包括之后上线的节点。
  不按名字删除 `-kata` 后缀，也不把不同 runner 的身份合并。离线旧注册仍留在 Servers。
- 既有定时实验仍显示它们的真实到期时间；记录列表的空到期时间显示 Until disabled。

## 持久化和采集

沿用现有 Start / Stop API、配置版本、后台同步，没有新增控制协议。
`POST /admin/agent/profiling-runs` 的 `duration_seconds: null`（或省略）创建持续实验。
显式正整数仍走原有 60–1800 秒定时实验校验。

持续实验 manifest 使用 `continuous: true`、显式 `expires_at: null` 和
`max_operations: 0`。这里的 0 表示持续模式没有累计样本额度；不能保留原有每进程 100 条
上限，否则会在按钮仍是 Enabled 时停止采集。静态环境 descriptor 不接受持续模式。
传输使用 JSON null，不发送 Infinity，也不用虚假的遥远到期日期。

采集器内部用无限单调期限表示不自动过期。原 manifest、操作范围、归属和配置版本照常
随父子进程和 sandbox manager 调用传递。有限期限的旧 context 仍取所有上限中的最小值。
缺少截止字段不等于无限期限：必须有明确的持续模式、null 值和合法 manifest。

迁移 `0021_continuous_measurement` 仅允许 run 表的 expires_at 为 NULL；旧行不变。
NULL 是持续实验的不可变身份。Stop 写终止状态，不改到期字段，不删除已有样本。
已有 NULL 行时降级迁移会明确失败，不能给历史实验编造期限；没有这些行时可直接降级。

## Disable、重启与故障

Disable 停止选择新的根操作，已经开始的操作继续完成与投递。关闭页面和服务重启均不
自动关闭测量，重启后从持久化配置恢复。各进程照旧通过后台同步收到开关变化；正常约
30 秒，不宣称即时停止。断联 producer 可能继续按最后配置采集，直到同步恢复；持续模式
不再有定时实验的到期兜底。显式部署级 MODE=disabled 仍拒绝新选择与接续。

有界导出队列、并发数、请求超时、重试次数和丢弃计数保持原值。持续采集不保证每个样本
必然保存，拥塞时仍按原规则丢弃并计数。测量只覆盖服务端边界，不主动执行业务操作。
历史样本仍持续累积，本改动不加入自动保留清理。持续 manifest 没有基于 expires_at 的
投递宽限截止；投递身份校验和在途请求的重试/超时仍生效。

## 发布与兼容

常规数据库迁移之后升级 CP、runner、sandbox manager 和 turn 子进程代码，不增加环境变量。
所有角色升级完成后再开启持续实验；旧 producer 不理解空截止，会拒绝该选择。
定时实验、静态配置、显式禁用、旧样本和筛选接口继续兼容。

## 验证要求

- 受控时钟前进一天、成功结束超过 100 个操作后仍能选择；Disable 后新操作不选，
  在途片段继续采集。context / manifest / record 均能用严格 JSON 编码。
- 真实新解释器继承持续选择，保留 measurement id、起点、run 和空截止；禁用进程拒绝。
- 真实 PostgreSQL + HTTP：启用 → CP / runner / sandbox manager 同步 → 文件请求采集并入库 →
  Admin 读回 → Disable；停止时已在途的记录完整入库。定时实验同一链路保持通过。
- 校验缺少期限、错误持续标志、非法额度仍被拒绝；NULL 与有限期限的 manifest 冲突仍拒绝。
- 数据库迁移 upgrade → downgrade → re-upgrade；前后端质量门和页面回归测试通过。

验收链使用真实 HTTP 和 PostgreSQL，文件执行由现有测试 sandbox manager 替身提供。
这不等于已完成 staging 上的真实沙盒性能验收。

## 本次本地验证

- `make check`：Ruff、pydocstyle、mypy（239 文件）通过，4,941 个非 DB 测试通过。
  首轮四并发时，一个未修改的部署脚本用例耗时 10.4 秒，超过其 10 秒断言；单独复跑
  和两并发全量重跑均通过，没有调整断言。
- 前端 lint / format / type-check 通过，1,766 个测试通过。
- PostgreSQL 16：控制验收链 8 个用例、存储 29 个用例通过；升级/回退/再升级验证通过。
  本轮只运行相关 DB 用例，不声称运行了全部 dbtest。两个专用测试数据库已删除。
