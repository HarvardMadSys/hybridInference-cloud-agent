# Settings 管理第一组实现与验收

日期：2026-10-28。基线：`dev@3f60b6afbb86c67422b3c237c3e9ab68ed2effac`。
工作分支：`codex/settings-management-design`。

关联设计：[设计文档](../specs/2026-10-28-settings-management-design.zh-CN.md)、
[DSH 细分执行卡](../specs/2026-10-28-settings-management-dsh-steps.zh-CN.md)。

## 范围与分工

Codex 与 Claude Code（已核实 `claude-opus-5-5`、high effort）讨论三轮，
确定管理入口、状态含义、恢复页面导航和根密钥回退边界。
DSH（DeepSeek v4.1 Flash / high）按 S01–S02、S03–S04、S05–S11 三批实施。
Codex 审查真实 diff、修正交互与测试、执行独立验证。DSH 的自述不作为通过证据。

本次完成 S01–S12，检查结果见下表。后续 S13–S29 尚未实现，
S30–S47 仍需各自 ADR 与实施设计，不能当作本批已有能力。
没有发布、部署、推送、PR、数据库迁移或真实凭据修改。

## 已实现行为

- 保留七个旧 key、顺序与兼容字段；新增稳定 ID、管理方式、必要性、动作与状态元数据。
- 元数据与状态逻辑移入窄模块 `deployment_setup.py`；不改设置存储、加密格式与鉴权。
- 数据库项只表示本次读取成功；其他状态只描述配置证据，不声称健康、连通或全进程生效。
- 根密钥区分 dedicated/fallback/missing；同字节分离与更换密钥所需重加密明确区分。
- 不探测网络、读取挂载密钥或重新解析 Compute 文件；单项状态异常降级为 unknown。
- Deployment 独立标签；搜索、稳定 hash、旧 hash 与草稿保留继续有效。
- GitHub/pgAdmin 动作定位到字段；Compute 动作打开已有 Server setup；文档目标固定。
- `/setup` 保留说明。跨页面管理入口在恢复上下文只显示说明，内存 token 不进入 URL 或存储。
- 旧响应仍显示原说明；未知动作不可导航；尚未实现的轮换、迁库没有伪操作按钮。

## Codex 审查修正

1. 初次异步加载必须等待 definitions 再处理旧 hash；未知 ID 回退到可见的 Deployment 标签。
2. 从 Deployment 跳到字段时更新 hash，避免刷新又返回旧部署项。
3. Compute 不能只定位到服务器列表：增加打开已有面板与关闭时清理 hash 的处理。
4. 移除“所有项目都只能启动时提供”的笼统文案；现有说明文档不冒称完整密钥轮换工具。
5. 新增状态失败隔离测试、真实页面的深链接/草稿测试及桌面/手机浏览器用例。
6. 新测试分开存放；共享纯 fixture，页面测试行数预算从 2333 降到 2296。
   没有提高文件大小上限、删除原有行为断言或降低门禁。

## 独立验证

执行地点：已授权的 staging 开发主机，Linux ARM64 独立临时 checkout；
没有使用线上服务或线上数据。腾讯开发机的已知登录路径不可用，本次没有该环境验收。
Python 3.13.12，Node.js 22.23.4。前端运行真实生产构建，API 使用生成的 fixture。

| 检查 | 结果 |
|---|---|
| 后端相关 settings / admin settings 测试 | 317 passed（随后新增失败隔离用例由完整门禁覆盖） |
| `make check`：format、lint、mypy、普通测试 | 12,408 passed，7 skipped；门禁通过 |
| Node 22 `make ci-frontend` | lint、format、types、4,418 个单测、server-only、两种 base path 的生产构建/运行时配置、browser-install 通过；browser 初次失败，修正后按下一行独立重跑通过 |
| 全套 Chromium 浏览器回归 | 14 passed，含 1440px / 390px Deployment 用例；复用同一份已通过的生产构建 |
| 最后修改的浏览器测试 | 单文件 ESLint、Prettier 复查通过 |
| 本地提交源文件与远端受测文件 SHA-256 对照 | 19 个代码/测试文件一致 |
| 数据库 tier | 本批无 storage、SQL、migration 或 DB test 改动，未运行 |

七项后端跳过项均为已有测试，需要 root / setpriv 才能切换用户，不是本批新增跳过。
首次完整检查的两个迁移工作流测试在未修改基线也失败；原因是继承 SSH 环境后，
非交互 Bash 重置 PATH，使测试内模拟的 uv 命令失效。仅在测试进程清除
`SSH_CLIENT` / `SSH_CONNECTION` 后通过，没有修改测试或主机全局配置。
一次 Git 子进程取消测试失败，单独重跑和最终完整检查均通过，保留其偶发失败记录。

前端中间轮次发现新增测试超过文件大小预算、测试异步读取时机及错误焦点假设，
均已修正并复查。测试用 Node 22 的绝对运行目录进入 PATH，避免嵌套 npx
继承调用参数造成浏览器安装失败；没有修改用户的 Node 默认版本。

最后一次全套前端命令的 browser 子项失败，是新测试把字段定位误当成自动进入编辑。
实际跳转正确展示并聚焦当前值；仍需组内 Edit 后编辑，通过 Review / Apply 保存。
浏览器用例改为验证此完整流程，14 项全部重跑通过。没有修改生产代码以绕过审核，
也没有把最后一次 `make ci-frontend` 的退出码写成 0；完成依据为上述逐项结果。
桌面与手机截图仅为 fixture 界面；不能作为真实部署或配置有效性的证明。

## 后续队列

下一组从 S13 开始：按实际读取点标注生效方式、Review 提示和操作文档。
S21–S29 再做 Common、通常只设置一次、缺项筛选与原地编辑。
管理员凭据、轮换、迁库和实例应用状态仍分别经过设计门；本记录不表示其已经实施。
