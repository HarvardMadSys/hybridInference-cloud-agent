# 可复用云端环境配置验收记录

日期：2026-10-13。对应[设计](../specs/2026-10-12-repository-environment-design.zh-CN.md)；本轮实现及验证以 `dev` 的 `d623fa69` 为基线，同一个功能 PR 提交全部配置、执行、UI、安全和测试改动。

## 已运行的质量门禁

| 检查 | 环境及结果 |
| --- | --- |
| `make check` | Linux、Python 3.12、Git/C 编译器/zstd；格式、lint、pydocstyle、mypy 通过，9,406 个测试通过、11 个跳过 |
| `make ci-frontend` | Node.js 22；eslint、prettier、TypeScript、Vitest、server-only 和 runtime-config 全部通过，3,710 个测试通过 |
| `make ci-database` | 每次新建 PostgreSQL 15，1,401 个测试通过、0 个跳过 |
| Docker 实际生命周期验收 | 10 个测试通过，无推理请求、无外部网络请求；见下文 |
| 仓库检查 | `git diff --check`、提交内容的凭据扫描通过 |

后端在 Linux 容器执行，因为进程冻结/清理测试需要真实 `/proc`。Git 元数据只读挂载，依赖由 `uv sync --frozen` 安装。11 个跳过来自已有测试：1 个需要 OpenSSH server parser，10 个需要 Docker Compose CLI；不是跳过本功能的新测试。数据库门禁使用独立临时数据库及四个 worker 数据库，不复用生产数据。

## PR #576 评审回归覆盖

- 非空短 Secret 在持久化前拒绝；`.env` 预览不回显值，需用户明确将 `DEBUG=1` 选为普通变量。多行短片段/通用头尾不误伤正常输出，完整值跨 chunk 仍被遮蔽。
- 在真实 Git 仓库执行生产扫描脚本，覆盖未跟踪、已暂存、已跟踪、二进制、符号链接、忽略文件与删除旧值；保持用户 index 字节不变。
- 空绑定固定旧任务选择但不要求环境协议；终端带有效配置时只有能力检查和终端创建两个 Sandbox Manager 请求，旧 Sandbox Manager 收不到值。
- 后台测试执行可取消、单次占一个槽位、不会同步阻塞调用线程，孤儿查询节流且不执行全局写库维护。
- 快捷操作保留 host 私有执行记录，回收和重启后的重试不重跑；缺失结果明确标为 unknown，迟到状态不能覆盖数据库终态；重复请求和结果写盘失败不会遗留工作区活动令牌。
- 普通标题字段仍返回原字段校验错误；未知 ValueError 不作为 422 回显；SourceControlError 不误报环境加密故障。
- 环境请求失败退出忙碌状态，编辑/仓库选择可用；恢复后不漏掉默认环境。

## 本功能覆盖

- 普通变量和 Secret、全局/环境/临时覆盖及来源、阶段过滤、保留名、数量/字节上限、原子 dotenv 解析、加密 domain 隔离和 Secret 不回显。
- 真实 PostgreSQL 的 owner 隔离、CAS/并发合并上限、不可变历史、复制幂等、恢复/归档/GC、固定版本及临时覆盖；原生 Fork 在轮换后仍继承源绑定和临时变量。
- 真实 HTTP 路由与 PostgreSQL 的“创建全局与仓库环境 → 测试领取/结果 → 新任务绑定 → 修改版本 → 新任务复用 → 删除”流程。验证 409 当前版本、输出脱敏、旧工作区版本不变、归档后仍可读取。此层仅替代外部 GitHub 元数据和 host 的结果上报，不声称运行了真实 agent。
- runner/Sandbox Manager 必须同时兼容；任一不兼容则归还任务，不发推理凭据、不消耗 attempt。测试领取还会重新确认 owner 的 Agent 使用权限。
- 独立环境测试的额度、host 槽位与 autoscaler 队列、lease、取消/完成竞争、清理确认；测试没有推理/MCP/仓库写凭据。
- 流式跨 chunk/多行脱敏、控制字段保护、脱敏幂等与截断；受信发布扫描识别二进制/历史 Secret，允许只删除既有 Secret 内容。
- 前端变量序列化、任务选择/临时覆盖、默认与基础环境选择，以及现有设置、工作区标签、菜单和页面的回归测试。
- 环境维护失败不会中断原有 attempt 或上传清理；修改既有迁移/fixture、数据库时钟与统计快照用法时保留原断言。日志导出测试用事件控制后台写入时序，避免把线程尚未调度当成测试前提。

## 实际 Docker 验收

执行 `tests/external/test_cloud_environments.py` 和 `tests/external/test_retained_command_limits.py`，确认：

1. 安装和启动阶段各收到正确变量；Secret 步骤日志被掩盖，安装期 detached 子进程被清理。
2. 同一实际 boot 不重复准备；容器停止后重启只重跑启动，服务在安装/启动返回后保持可用。
3. 操作在指定子目录执行；停止会清理其 detached 子进程，保留另一身份的进程。
4. 快照冻结及解冻包含启动服务与手动操作身份，使用相同 UID 信号，不增加 capabilities。
5. 一次性命令超时和输出溢出只清理该命令，保留容器及运行中的 sibling。
6. 操作写入计数文件后，回收终端并重建管理器；同一运行重试的计数仍为 1，退出码和脱敏输出从持久记录恢复。

镜像来自本地缓存 sandbox，在构建时把工作区/agent 目录所有权调整为当前生产 Dockerfile 的设置，并复制 Linux uv 0.9.30、公开下载 Python 3.12.12 到只读种子目录；测试通过 uv sync --offline 在真实工作区创建 Python 3.12 虚拟环境。执行时使用 non-root、`--cap-drop ALL` 和 no-new-privileges；测试容器采用 `network=none`，不代表重新验证了生产公网出口。容器均由独立标签与名称创建并清理。

## 仍需真实部署验证的边界

没有执行真实 Kata 多租户验收，也没有在真实 host 上分别运行五个 Agent CLI 的变量读取、平台凭据、native resume/Fork 验收；本地 Docker、单元测试和 API/数据库测试不替代这些验证。没有部署或修改生产环境，也未执行带真实第三方凭据的脚本。

`OPENAI_API_KEY` 与 `ANTHROPIC_API_KEY` 保留，用户自带 agent 账号不在设计范围内。Secret 是用户主动授予其代码和 agent 的能力；已知值脱敏及受信发布扫描不保证阻止编码外发、原始终端/文件读取、模型上下文或直接 Git push。安装和操作使用 /workspace/.cloud-agent-tools 下持久、可执行且组共享的工具 HOME 和数据目录；成员平台凭据 HOME 保持私有。PID 和状态文件仍在 noexec 私有 tmpfs。

控制面所有副本升级后才可新建绑定，runner/Sandbox Manager 要共同支持 `environment-v1`；已有绑定不支持直接回退到不理解绑定的旧程序。完整升级/回滚及 key 备份要求见[运维说明](../../developer/operations.md#reusable-cloud-environments)。

## 后续评审回归与产品恢复入口

- 前景安装/验证快速非零退出与缺失回执不会显示成功；服务持续输出不再重写已完成回执。
- 过期测试不再永久占槽；cleanup 未完成的返回 host 在确认清理前不能领新任务。领取前密钥失败通过无需解密的事务释放，出版解密暂时失败保持重试。
- 注册/心跳仅在实际共享容器支持完整时宣告环境能力；同幂等键重试不重新访问 GitHub 或解析移动后的分支。
- 空覆盖行不提交；初次列表加载失败仍可提交并省略环境字段，由服务器解析默认配置。
- GET 状态不写库，Docker inspect 在线程中并有短缓存；脱敏缓存按 owner、运行期版本和加密 key 隔离，GC 每小时且有反向引用索引。准备摘要由实时租约事务记录，旧 host 不可覆盖。
- 用户明确更新运行期变量，下一轮生效且保留安装输入；CAS、owner 权限、历史 Secret 扫描、空绑定在关闭开关后不可升级均有真实数据库回归。
- 启动失败默认阻止；用户明确授权后仅下一次 human 轮次可跳过，不能由 goal 或兄弟成员消费，不记录永久成功。
- 默认关闭部署开关；已有绑定继续执行。实际执行 migration downgrade，证明未使用环境功能的空绑定可以回退，实际配置和未清理测试会阻止回退。
- 真实 Docker 验证安装/验证 exit 7/9、Python 3.12 虚拟环境被另一成员执行、重启后不重装、安装 UID 与操作 UID 共用工具目录及 Git 排除。

67 个额外 tmpfs 在 Kata 下的启动成本尚未实测；活跃配置旧 Secret 历史仍保留，没有立即物理清除入口；脱敏器逐模式处理的成本没有改为正则。以上保留在现有边界内，不宣称本次已经解决。现场 Kata/五 CLI 验收完成前，部署开关保持关闭。

## 最新 dev 合并兼容

已合入 `d623fa69`，保留 dev 的 Sandbox Manager 重命名、会话 slot 回收、Claude resident turn 和 MCP 修复。环境 host 实现移入 `cloud_agent_host/sandbox_manager.py`；旧 `workspace_broker.py` 保留 dev 的兼容入口。控制面路由、能力检查、客户端错误类型与回归测试使用新名称。

环境迁移改为 `0065_environments`，接在 `0064_mcp_remembered_approvals` 后；Alembic 只有一个 head。完整数据库门禁验证升级、既有 MCP 迁移断言、环境配置和回退守卫。

容器验收在本机 Colima 执行；临时测试副本只替换本地 daemon socket 和副本互相引用，生产执行路径与所有断言保持原样。10 个测试实际通过，未跳过。
