# 网页只挂在根路径：去掉子路径挂载

- 日期：2026-11-04
- 依据：[精简 `.env`](2026-11-03-slim-env.zh-CN.md)、[首次安装自动生成密钥](2026-11-04-first-install-keys.zh-CN.md)
- 前置：FreeInference 的发版检查（`ops/resolve_agent_release.py`）先改成接受没有 `/agents` 网页镜像的发版，
  合并并上线后，本改动才能合并。

## 1. 为什么

以前网页可以挂在别的网站的某个路径下（`freeinference.org/agents`），所以每次发版都构建两个网页镜像
（根路径一个、`/agents` 一个），部署要在 `.env` 里写 `AGENT_WEB_BASE_PATH` 和 `AGENT_WEB_API_URL`，
compose 还故意不给默认值，因为两种挂法填错了都不会马上报错。

FreeInference 从 2026-09-11 起改用独立域名（`agents.freeinference.org`），staging 和 production 都用根路径镜像，
`/agents` 镜像没人用了。留着它只会让 `.env` 多两行、每次发版多构建一个镜像、代码里到处是拼前缀的逻辑。

## 2. 改了什么

| 部分 | 改动 |
|---|---|
| 网页代码 | 去掉 `basePath` 和所有手工拼前缀的地方；浏览器 API 路径固定为 `/api`，构建时相对路径只接受 `/api`，绝对 URL 仍可用于跨源部署 |
| 网页镜像 | 不再有 `NEXT_PUBLIC_BASE_PATH` 构建参数；`io.hybridinference.artifact.base_path` 标签保留，恒为空，FreeInference 的解析器会核对它 |
| compose 和部署脚本 | compose 不再读这两个变量；`deploy.sh` 的网页健康检查探测 `/`；`.env` 里还留着有值的 `AGENT_WEB_BASE_PATH` 时，`deploy.sh` 和发版部署脚本都会拒绝，而不是悄悄忽略 |
| 发版 | 目录里删掉 `web-agents`，只构建根路径镜像；冒烟、验收、CI 都只跑根路径 |
| 清单格式 | 仍是 v1。新发版只产出根路径镜像；格式继续接受已发布清单里的 `agents` 镜像（`manifest_contract.retired_web_variants`），保证回滚到旧版本时清单检查照样通过 |
| 示例 `.env` | 删掉两行网页路径和 `AGENT_DEPLOYMENT_SHA`（`deploy.sh` 自己导出），网络名改为可选。控制面示例只剩 4 行，用自带数据库时都由 `deploy.sh` 生成 |
| 其他 | 删掉 `deploy/reverse-proxy/agents-under-parent-site.conf` 及其测试；变量登记表里 `AGENT_WEB_BASE_PATH` 改为“已退役，只为拒绝而读” |

## 3. 兼容性

- **FreeInference**：它的配置写的是 `AGENT_WEB_VARIANT=root`、`AGENT_WEB_BASE_PATH=`（空），新脚本都接受。
  唯一要改的是它的发版检查要求两种网页镜像都在，所以那边的 PR 必须先合并上线。
- **回滚**：旧发版的清单带着 `/agents` 镜像，格式和 FreeInference 的检查都继续接受；部署时只用根路径镜像。
- **还在用子路径挂载的部署**：本仓库和 FreeInference 都没有。万一有，`deploy.sh` 会明确拒绝并说明原因。

## 4. 验证

- 前端：去掉前缀后的登录跳转、API 前缀检查、安全响应头、构建检查脚本的测试。
- 发版：目录、清单格式（旧 v1 样例仍合法、只有根路径的新清单合法、缺根路径被拒）、发版环境、编排、冒烟、验收的测试。
- 部署：残留的 `AGENT_WEB_VARIANT=agents` 或 `AGENT_WEB_BASE_PATH=/agents` 在任何服务启动前被拒绝。
