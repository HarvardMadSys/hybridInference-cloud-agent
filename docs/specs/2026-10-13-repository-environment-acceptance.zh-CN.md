# 可复用云端环境配置验收记录

日期：2026-10-13。对应[设计](2026-10-12-repository-environment-design.zh-CN.md)；实现以 `dev` 的 `542249c6` 为基线，同一个功能 PR 提交全部配置、执行、UI、安全和测试改动。

## 已运行的质量门禁

| 检查 | 环境及结果 |
| --- | --- |
| `make check` | Linux、Python 3.12、Git/C 编译器/zstd；格式、lint、pydocstyle、mypy 通过，8,904 个测试通过、9 个跳过 |
| `make ci-frontend` | Node.js 22；eslint、prettier、TypeScript、Vitest、server-only 和 runtime-config 全部通过，3,513 个测试通过 |
| `make ci-database` | 每次新建 PostgreSQL 15，1,309 个测试通过、0 个跳过 |
| Docker 实际生命周期验收 | 5 个测试通过，无推理请求、无外部网络请求；见下文 |
| 仓库检查 | `git diff --check`、提交内容的凭据扫描通过 |

后端在 Linux 容器执行，因为进程冻结/清理测试需要真实 `/proc`。Git 元数据只读挂载，依赖由 `uv sync --frozen` 安装。9 个跳过来自已有测试：1 个需要 OpenSSH server parser，8 个需要 Docker Compose CLI；不是跳过本功能的新测试。数据库门禁使用独立临时数据库及四个 worker 数据库，不复用生产数据。

## 本功能覆盖

- 普通变量和 Secret、全局/环境/临时覆盖及来源、阶段过滤、保留名、数量/字节上限、原子 dotenv 解析、加密 domain 隔离和 Secret 不回显。
- 真实 PostgreSQL 的 owner 隔离、CAS/并发合并上限、不可变历史、复制幂等、恢复/归档/GC、固定版本及临时覆盖；原生 Fork 在轮换后仍继承源绑定和临时变量。
- 真实 HTTP 路由与 PostgreSQL 的“创建全局与仓库环境 → 测试领取/结果 → 新任务绑定 → 修改版本 → 新任务复用 → 删除”流程。验证 409 当前版本、输出脱敏、旧工作区版本不变、归档后仍可读取。此层仅替代外部 GitHub 元数据和 host 的结果上报，不声称运行了真实 agent。
- runner/broker 必须同时兼容；任一不兼容则归还任务，不发推理凭据、不消耗 attempt。测试领取还会重新确认 owner 的 Agent 使用权限。
- 独立环境测试的额度、host 槽位与 autoscaler 队列、lease、取消/完成竞争、清理确认；测试没有推理/MCP/仓库写凭据。
- 流式跨 chunk/多行脱敏、控制字段保护、脱敏幂等与截断；受信发布扫描识别二进制/历史 Secret，允许只删除既有 Secret 内容。
- 前端变量序列化、任务选择/临时覆盖、默认与基础环境选择，以及现有设置、工作区标签、菜单和页面的回归测试。
- 环境维护失败不会中断原有 attempt 或上传清理；修改既有迁移/fixture、数据库时钟与统计快照用法时保留原断言。

## 实际 Docker 验收

执行 `tests/external/test_cloud_environments.py` 和 `tests/external/test_retained_command_limits.py`，确认：

1. 安装和启动阶段各收到正确变量；Secret 步骤日志被掩盖，安装期 detached 子进程被清理。
2. 同一实际 boot 不重复准备；容器停止后重启只重跑启动，服务在安装/启动返回后保持可用。
3. 操作在指定子目录执行；停止会清理其 detached 子进程，保留另一身份的进程。
4. 快照冻结及解冻包含启动服务与手动操作身份，使用相同 UID 信号，不增加 capabilities。
5. 一次性命令超时和输出溢出只清理该命令，保留容器及运行中的 sibling。

镜像来自本地缓存 sandbox，仅在构建时把工作区/agent 目录所有权调整为当前生产 Dockerfile 的设置。执行时使用 non-root、`--cap-drop ALL` 和 no-new-privileges；测试容器采用 `network=none`，不代表重新验证了生产公网出口。容器均由独立标签与名称创建并清理。

## 仍需真实部署验证的边界

没有执行真实 Kata 多租户验收，也没有在真实 host 上分别运行五个 Agent CLI 的变量读取、平台凭据、native resume/Fork 验收；本地 Docker、单元测试和 API/数据库测试不替代这些验证。没有部署或修改生产环境，也未执行带真实第三方凭据的脚本。

`OPENAI_API_KEY` 与 `ANTHROPIC_API_KEY` 保留，用户自带 agent 账号不在设计范围内。Secret 是用户主动授予其代码和 agent 的能力；已知值脱敏及受信发布扫描不保证阻止编码外发、原始终端/文件读取、模型上下文或直接 Git push。安装依赖应放入共享工作区；每个准备/操作身份的私有 HOME 不用于跨身份共享依赖。

控制面所有副本升级后才可新建绑定，runner/broker 要共同支持 `environment-v1`；已有绑定不支持直接回退到不理解绑定的旧程序。完整升级/回滚及 key 备份要求见[运维说明](../operations.md#reusable-cloud-environments)。
