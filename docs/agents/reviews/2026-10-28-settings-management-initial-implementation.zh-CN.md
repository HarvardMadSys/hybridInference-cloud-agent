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
没有使用线上服务或线上数据。首轮未连接腾讯开发机；后续查明当时使用了错误用户名，
按历史成功记录显式指定完整账号后已连接，补验收见本文末节。
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

## 腾讯开发机补验收（2026-10-28）

查阅 10 月 17 日、20 日的原始成功命令后，显式指定正确的完整账号，使用现有
SSH 配置即可连接。首轮失败由错误用户名造成，不能据此断言密钥失效。
准确连接方法已按用户要求写入本机全局 Codex `AGENTS.md`，未把私有连接信息放入仓库。

在已确认的腾讯开发机新建独立 checkout，分支 `codex/settings-management-design`，
提交 `9beb69aeff544aba7c521e3ab625ff0e735ab478`。Linux x86_64，Python 3.12.11，
Node.js 22.23.3；19 个改动代码/测试文件的 SHA-256 与本地受审查版本一致，工作区干净。

| 检查 | 结果 |
|---|---|
| `make check PYTEST_WORKERS=4` | 退出码 0；format、lint、mypy 通过；12,408 passed、7 个已有 root-only skips |
| Node 22 `make ci-frontend` | 退出码 0；八个子项全部 PASS |
| 前端 Vitest | 274 files、4,418 passed |
| Chromium | 14 passed，包含 1440px 与 390px 的部署入口、深链接、草稿回归 |
| Compose 相关补查 | 159 passed |

第一次后端检查发现 6 个 Compose 调用失败、另有 2 个 Compose 用例跳过，原因是
默认 Docker CLI 没有可发现的 Compose 插件。复用之前测试目录中已有的 Compose
2.39.4，仅在本次临时 `DOCKER_CONFIG/cli-plugins` 建立引用后，相关补查和完整
门禁均通过。没有修改系统包、全局 Docker 配置、SSH 授权或生产环境。
前端复用已有隔离 Chromium 运行库；进程内的 PATH / OPENSSL_CONF 等环境调整
不改变主机默认配置，TLS 证书和主机名校验仍开启。

本节补齐腾讯环境验证，不改变前文的实现范围或 fixture 验收边界；未推送或部署。

## 预览反馈：取消 Advanced 折叠（2026-10-28）

用户在 PR #1034 的预览中要求去掉 Advanced 折叠。后续实现提交 `43520704`：

- 应用设置按实际功能分组，每组直接展示全部字段，取消 Important/Advanced 拆分；
  同一功能只保留一个编辑器，原有 Edit / Review / Apply、CAS、secret 替换与只读规则保留。
- CloudLab、归档上传限制、已安装版本和主机设置同样直接展示，标题描述实际内容。
- 搜索和旧深链接仍定位字段，切换标签和轮询仍保留草稿。删除的是折叠状态及展开逻辑，
  没有删除字段级重置菜单、校验或权限检查。
- 同步设计：数据库连接编辑需要独立于数据库的配置来源与应用流程；修改同库连接参数
  不等于迁移数据库。Deployment 中的文档链接并不提供此编辑能力。

DSH 完成前端和测试适配草案后，Codex 接手审查与独立验收。首轮相关测试中 4 项断言
仍混淆了“当前标签不可见”和“折叠不可见”，已按新交互修正，保留焦点、校验和持久化断言。
没有提高测试大小上限；三处文件预算分别降到 2295、1909、1339 行。

在同一腾讯开发机隔离 checkout 上应用受审查补丁，19 个修改文件的 SHA-256 与本地
提交内容逐项一致。Node.js 22.23.3，实际结果：

| 检查 | 结果 |
|---|---|
| 设置及主机相关测试 | 12 files、311 passed |
| `make ci-frontend` | 退出码 0；八个子项全部 PASS |
| 全部前端单测 | 274 files、4,418 passed |
| Chromium 浏览器回归 | 16 passed；新增桌面/手机字段直接可见与跨标签草稿用例 |
| 缩小后的三个测试文件预算 | 单独 ESLint 复查通过 |

本轮没有后端代码改动，后端沿用前节完整检查结果。预览使用这次通过检查的真实前端构建
与示例 API；未改变真实数据库连接、凭据或生产部署。
