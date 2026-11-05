# 首次安装自动生成密钥

- 日期：2026-11-04
- 依据：[精简 `.env`](2026-11-03-slim-env.zh-CN.md) 第 7 节最后一条；本设计叠在 #1092 之上，在它之后合并
- 实现：`deploy/control-plane/deploy.sh`（`ensure_first_install_keys`）、
  `apps/backend/cloud_agent/settings.py`（`first_settings`、`GENERATED_ON_FIRST_START`）、
  `deploy/local/dev.sh`（一处兜底修正）

## 1. 要解决的问题

#1092 之后，控制面的 `.env.example` 还有 9 行有效配置，其中 5 行要部署者自己生成或填写：数据库连接、
数据库口令、设置根密钥、账号会话密钥和 `ADMIN_TOKEN`。新部署者照着示例填完，还会撞上一个示例没写的坑：
控制面启动时检查用户凭据加密密钥（`bootstrap/identity.py` 的 `_check_model_connection_key`），新建的设置行里
这把钥匙是空的，控制面直接拒绝启动，`/setup` 页面也就打不开。要让它起来，部署者得知道去 `.env` 里加一行
示例里已经删掉的 `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY`。

另一方面，`deploy.sh` 目前只会生成一把钥匙：`AGENT_COMPUTE_SECRET_KEY`。可这一把恰恰在首次启动后就没用了，
它被导入设置行，之后以数据库为准。

目标：**用自带数据库、用 `deploy.sh` 部署的新安装，`.env` 里不需要手填任何密钥；同时，任何已有部署都不能
因此失去解密已存数据的能力。**

## 2. 原则

1. **已有的值一律复用，绝不替换。** `.env` 里有值，或者执行 `deploy.sh` 的进程环境里有值，都算「有」。
   Compose 插值时进程环境优先于 `.env`，所以两处都要看。
2. **数据库之前就要用的钥匙，只在能证明是全新安装时生成。** 设置根密钥解密所有存进数据库的秘密，给已有部署
   换一把新的，等于把这些秘密全部锁死。证明不了就停下来，说清楚缺什么、为什么不生成，绝不猜一个值。
3. **数据库里的钥匙由应用自己生成，不再写进 `.env`。** 它们本来就是设置项，由设置根密钥加密存放，
   放在 `.env` 里反而让人以为改那一行有用。
4. **生成的值不进日志、不进命令行参数。** 日志只写变量名。

## 3. 什么在哪里生成

| 值 | 谁生成 | 什么时候 | 存在哪 |
|---|---|---|---|
| `AGENT_POSTGRES_PASSWORD` 和 `AGENT_DATABASE_URL` | `deploy.sh` | 两者都空，且是全新安装 | `.env`；连接串指向自带的 `postgres` 服务，用户和库名取 `AGENT_POSTGRES_USER`/`AGENT_POSTGRES_DB`，默认 `cloudagent` |
| `AGENT_LOCAL_SESSION_SECRET` | `deploy.sh` | 为空，且是全新安装 | `.env` |
| `AGENT_SETTINGS_ENCRYPTION_KEY` | `deploy.sh` | 为空、`AGENT_SESSION_SECRET` 也为空，且是全新安装 | `.env` |
| 主机签名密钥 `control_signing_secret`、用户凭据加密密钥 `source_control_encryption_key`、compute 凭据加密密钥 `compute_encryption_key` | 控制面 | 第一次创建设置行时，环境没有提供对应变量，且有可用的设置根密钥 | 数据库设置行，用设置根密钥加密 |

所有钥匙都是 32 字节随机数的十六进制（64 个字符）。

**不生成的**：

- `ADMIN_TOKEN`：第一次登录已经改用控制面日志里的一次性设置码（`/setup/administrator`），这个令牌只是
  「没有管理员能登录时」的后门。没设置就什么门都不开，这比常年放着一个生成的管理员令牌更安全。所以把它挪到
  示例的可选部分，不生成。
- 沙箱管理凭据 `sandbox_manager_token`：每台主机的 `.env` 里要有同一份。它在 Admin 里只写不读，控制面生成了，
  部署者也拿不到去配主机，只能再换一个。托管主机本来就由控制面写入。
- `AGENT_DEPLOYMENT_SHA`：`deploy.sh` 每次都会导出，`.env` 里那行本来就空着。
- `AGENT_WEB_BASE_PATH`、`AGENT_WEB_API_URL`、`AGENT_NETWORK_NAME`：有默认值，按站点情况改。

`deploy.sh` 不再写 `AGENT_COMPUTE_SECRET_KEY`，原来的 `ensure_compute_secret_key` 删掉。

## 4. 怎样算「全新安装」

`deploy.sh` 只有在下面几条都成立时才生成：

1. 部署目录里没有 `.deployed-sha`。这个文件只在一次部署完整成功后才写。
2. `AGENT_DATABASE_URL` 为空，或者指向自带的 `postgres` 服务（形如 `@postgres/` 或 `@postgres:5432/`）。
   外部数据库里有没有数据，部署脚本看不到。
3. Docker 能正常回答，并且这个 Compose 项目既没有数据库卷，也没有任何容器。项目名两处都查：命令行或默认的
   项目名，以及 `.env` 里的 `COMPOSE_PROJECT`。卷既按 Compose 标签查，也按名字 `<项目>_agent-postgres` 查，
   因为手工恢复出来的卷可能没有标签。Docker 查询失败，一律当作「不是全新」。
4. 只对设置根密钥：`AGENT_SESSION_SECRET` 也没有设置。

证明不了时怎么办：

- 缺的里面有 Compose 必填项（数据库连接、口令、账号密钥），就停止部署，报出缺哪几项、为什么没有生成，并提醒
  「已有数据库请填原来的值，不要填新的」。这些项缺了，Compose 本来也会因为 `${…:?}` 失败，所以不会让一个
  原来能部署的环境变得不能部署。
- 只缺设置根密钥，就只打一条警告，部署继续。Compose 不要求它，私有 override 的 `environment:` 也可能提供它；
  控制面会在 Admin 顶部提示它缺失。

数据库连接只给了一半（有连接串没口令，或者反过来），也停止部署：连接串里的口令可能经过 URL 编码，不能
从一边推出另一边。

生成的值这样写进 `.env`：空的 `NAME=` 行就地填上，没有这一行就追加到末尾；先写到同目录下 0600 的临时文件，
再改名覆盖。值通过环境变量交给 `awk`，不出现在别的用户能看到的命令行里。

这一步放在所有拒绝检查（部署协议、已退役的 Gateway 变量、override 路径）之后、第一次调用 Compose 之前，
所以一次被拒绝的部署不会先写下密钥。`abandon` 阶段不生成任何东西。

读 `.env` 时故意放宽：`export NAME=`、等号两边有空格，都按「有值」处理。宁可把一个写法奇怪的值当成「有」，
也不能把它当成「缺」再去生成。

## 5. 为什么已有部署是安全的

| 情况 | 结果 |
|---|---|
| 所有值都在（`.env` 或部署进程环境里） | 直接返回：不调 Docker，不碰文件 |
| 老部署靠 `AGENT_SESSION_SECRET` 兜底，没有 `AGENT_SETTINGS_ENCRYPTION_KEY` | 兜底在用就不算缺，绝不在旁边加一把新的设置根密钥（加了就会取代兜底） |
| 部署过，缺数据库连接、口令或账号密钥 | 停止并说明，不生成 |
| 部署过，缺设置根密钥、也没有兜底 | 只警告，不生成。一个正在运行的部署不可能处于这种状态：没有至少 32 字节的根密钥，任何加密设置都存不进去，而控制面启动必须有用户凭据加密密钥，它本身就是加密设置。所以根密钥一定来自部署脚本看不到的地方，比如私有 override 的 `environment:`，那里的值优先于 `.env` 插值，脚本也就不该动 |
| 以前靠 CI 之类临时导出值，这次手工运行没有导出 | 看起来「缺」，但 `.deployed-sha`、数据库卷或容器会证明不是新安装，于是停止或警告，不会生成新的根密钥。这正是需要「全新」证明的原因 |
| 外部数据库 | 证明不了是新库，不生成 |
| 已有设置行里的钥匙（哪怕是空值） | 应用不动：只有数据库里还没有设置行时才生成 |
| 两个进程同时创建设置行 | `seed_settings` 合并时以先存进去的为准，两边最终读到同一组钥匙；MCP 中继从不创建设置行 |
| 没有设置根密钥时创建设置行 | 不生成（加密不了），设置页照旧提示缺根密钥 |
| `.env` 里留着 `AGENT_COMPUTE_SECRET_KEY` | 无影响：它早已导入数据库，之后以数据库为准。删掉也不会再被补回来，已存的 compute 凭据照样能解密 |

同样的问题在本地栈也有一处：`deploy/local/dev.sh` 升级旧的 `.env.local` 时，缺 `AGENT_SETTINGS_ENCRYPTION_KEY`
就追加一把新的。旧的 Gateway 时代本地环境会先被拒绝，所以实际碰不到，但规则应当一致：现在 `.env.local` 里有
`AGENT_SESSION_SECRET` 时不再追加。

## 6. 部署者现在要填什么

| | 之前（#1092） | 之后 |
|---|---|---|
| 示例里的有效行 | 9 行 | 8 行（`ADMIN_TOKEN` 移到可选部分） |
| 要手工生成或填写的 | 5 行：数据库连接、口令、设置根密钥、账号密钥、`ADMIN_TOKEN`；另外还要知道补一行示例里没有的用户凭据加密密钥，否则控制面起不来 | 0 行（自带数据库，用 `deploy.sh` 部署） |
| 用外部数据库 | 同上 | 4 行：数据库连接、口令、账号密钥、设置根密钥（证明不了是新库） |

剩下的 8 行里，4 行空着由 `deploy.sh` 填，`AGENT_DEPLOYMENT_SHA` 空着由 `deploy.sh` 导出，3 行接线配置有默认值。
第一位管理员登录后，Admin 顶部仍会提示两项：网站地址和沙箱管理凭据（主机要同一份）。

新安装的步骤变成：复制示例为 `.env` → 运行一次 `deploy.sh --phase prepare`（生成钥匙、构建或拉取镜像）→
启动控制面和网页 → 用日志里的设置码在 `/setup/administrator` 创建管理员 → 在 Admin 里填网站地址和沙箱管理凭据
→ 接入主机，正常部署。`.env` 从此要和数据库一起备份。

## 7. 没做的

- **外部数据库的「新库」判断。** 要先连上数据库看有没有设置表，`identity_namespace --check` 是类似的思路，
  但它需要 Compose 能渲染，而 Compose 又要先有这些值。等真有人用外部数据库新装时再做。
- **沙箱管理凭据。** 理由见第 3 节；要做的话得先让主机能从控制面取到它。
- **不经过 `deploy.sh`、直接手工 `docker compose up` 的新安装。** 这条路仍要自己填四个值。文档改成先跑一次
  `deploy.sh --phase prepare`。
- **主机侧 `.env`**（接入令牌、沙箱管理凭据、隧道）：不在本次范围。
- **不主动删除已有部署 `.env` 里的 `AGENT_COMPUTE_SECRET_KEY`。** 升级说明写明可以删。
- **不改 Compose 文件里 `${…:?}` 的提示文字。** 手工 Compose 的人仍然看得到该怎么生成。

## 8. 验证

- 部署脚本：`tests/unit/deploy/test_first_install_keys.py` 跑打包后的 `deploy.sh`，覆盖新安装生成并持久化、
  第二次运行原样复用、已有值不被替换（含进程环境提供的值和 `export NAME = '…'` 写法）、靠
  `AGENT_SESSION_SECRET` 兜底的部署绝不得到新的设置根密钥（新装和已部署两种）、六种「可能有数据」的情形
  一律拒绝且 `.env` 不变、已部署但没有任何根密钥只警告、连接只给一半时拒绝、`abandon` 不写钥匙。
- 设置：`tests/unit/test_settings_first_start_keys.py` 覆盖首次建行生成三把钥匙并通过启动检查、重启后不再
  生成、环境提供的值原样导入、已有设置行（含空值）不生成、没有根密钥不生成、用兜底根密钥时照样加密、两个进程
  抢着建行时以先存的为准。
- 本地栈：`tests/unit/test_local_dev_script.py` 新增有 `AGENT_SESSION_SECRET` 的 `.env.local` 不追加设置密钥。
- 门禁：环境变量分类表的说明随之更新，`test_env_classification.py`、`test_env_examples.py` 保持通过。
