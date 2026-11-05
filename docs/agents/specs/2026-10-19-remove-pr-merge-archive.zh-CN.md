# 去掉"PR 合并后自动归档"

日期：2026-10-19。状态：已决定；实现和本文在同一个 PR 里。

设计核对基线：远端 `dev`，提交 `54bcc96f`。

相关：[按需发布](2026-10-15-manual-publication-design.zh-CN.md)、[对话归档与永久删除](2026-10-13-chat-archive-and-permanent-deletion-design.zh-CN.md)。

## 1. 结论

平台不再因为 PR 合并而归档对话。归档只在用户自己点归档时发生。

这次删掉后台 worker、它的存储代码、六个 Admin 设置和相关文档。`agent_pr_archive_checks` 表这次先不删，等这一版上线后，由下一个 PR 用迁移删掉（见第 4 节）。

## 2. 原来的行为

#444（2026-10-11）加了这个功能，默认开启：

1. 控制平面每 30 秒找出最新一轮成功、并且发布过 PR 的对话，给每个"对话 + PR"记一条检查（`agent_pr_archive_checks`）。
2. worker 用 GitHub App 或发布令牌查这个 PR 是否已合并。
3. 已合并，并且对话没有正在运行或排队的工作，就归档这个对话、暂停它的目标，并写一条 `session_archived_pr_merged` 事件。

## 3. 为什么去掉

- **没人要求就改了用户的对话**。2026-10-15 已经定下平台不主动推分支、不主动开 PR（见[按需发布](2026-10-15-manual-publication-design.zh-CN.md)）。PR 合并是 GitHub 上的外部事件，据此把对话从侧边栏移走，同样是替用户做决定。
- **PR 合并了，对话不一定结束了**。改成按需发布后，常见的用法是：合并第一个 PR，在同一个对话里接着做，再点一次 Create PR。用户也可能回来追问，或者排查合并后出的问题。
- **代价不小**。一个常驻 worker、一张检查表、每 30 秒一次 GitHub 请求，还有 lease、回执和重试这一整套。它出过问题以后还要继续修（例如 #665 想恢复因为权限被拒而停下的检查）。

想保持侧边栏整洁，用户自己归档就行。以后如果要在对话列表里显示"PR 已合并"，那是另一个设计，不和归档绑在一起。

## 4. 为什么表分两步删

`agent_pr_archive_checks` 里存着"这个对话已经因为这个 PR 归档过"的回执。旧版本靠这些回执避免把用户恢复过的对话再归档一遍。

如果这次就删表，回滚时 downgrade 只能重建一张空表。旧版本的 worker 看不到回执，会把所有 PR 已合并的对话重新归档，包括用户已经手动恢复的。先只删代码，表原样留着，回滚就没有这个问题。删旧 memory 表也是这样做的：#643 先停止读写，#652 再删表。

下一步：这一版上线以后，加一个迁移 `DROP TABLE agent_pr_archive_checks`，downgrade 重建空表，并在升级说明里写明回滚到更早版本时回执已经没有了。

## 5. 改动

- 删除 `workers/pr_archive.py`、`storage/repositories/pr_archive.py`、`ports/pr_archive.py`，以及 `bootstrap/lifespan.py` 和 `storage/assembly.py` 里的接线。
- 删除设置 `pr_archive_enabled`、`pr_archive_poll_s`、`pr_archive_timeout_s`、`pr_archive_batch_size`、`pr_archive_retry_max_s`、`pr_archive_max_failures`，以及 Admin 设置页的 "Archive after merge" 分组。
- 删除 Compose 里的 `AGENT_PR_ARCHIVE_ENABLED`、`AGENT_PR_ARCHIVE_POLL_S`，以及 `ENVIRONMENT.md` 里对应的一节。
- 删除开发者文档 `automatic-pr-archiving.md` 和它的中文翻译，去掉其他页面指向它的链接。
- 删除这个功能自己的测试，并从启动、关闭和存储装配的测试里去掉它。

## 6. 升级和兼容

- **已经被自动归档的对话**保持归档，用户可以照常恢复。历史里的 `session_archived_pr_merged` 事件保留，界面不读它。
- **保存过的设置**：数据库里已有的 `pr_archive_*` 值不清理。读取设置时只看当前注册的设置项，多出来的值会被忽略。回滚到旧版本时，管理员原来的选择（比如关掉过这个功能）还在。
- **环境变量**：部署里如果还设着 `AGENT_PR_ARCHIVE_*`，新版本不再读它们，可以删掉。
- **滚动升级**：新旧版本短暂共存时，旧版本的 worker 照常工作，直到被替换。新版本不碰那张表。
- 不需要数据库迁移。
