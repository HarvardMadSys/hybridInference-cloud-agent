# 环境变量盘点与最小 CI 门禁（P1 修正版）

- 日期：2026-10-29
- 基线：Cloud Agent `dev@4b7b826d`（#1034 合并后）
- 参照：HybridInference `refs/codex/env-inventory-reference`（`3098cb9f`）中的
  `apps/backend/serving/config/app_config_registry.py`、
  `tests/unit/config/test_app_config_registry.py`、`tests/unit/config/test_no_raw_env_reads.py`
- 性质：盘点 + 门禁实现。本 PR 不迁移任何产品设置，不改默认值、安全策略或网络策略。
- 实现：`tests/support/env_scan.py`（扫描器）、`tests/support/env_inventory.py`（分类表）、
  `tests/unit/test_env_classification.py`（门禁）、`tests/unit/test_env_scan.py`（扫描器自测）
- 验证记录：[2026-10-29-env-inventory-gates.md](../reviews/2026-10-29-env-inventory-gates.md)

## 1. 结论

**已经在 Admin 里的**：`SETTINGS` 中带环境变量名的 64 项（其中 20 项在 `MIGRATED_SETTINGS`），
以及经心跳下发的 23 项 `HOST_SETTINGS` 和每台主机的 slots。这些变量今天在部署环境里只起
「首次导入」或「主机收到第一次心跳应答之前的引导值」的作用。

**现在就能按常规方式搬进 Admin 的**只有两项：

- `AGENT_MAX_STREAMS_PER_USER`：只有控制面读，每次占用流时现读，注册进 `MIGRATED_SETTINGS` 即可。
- `AGENT_LEASE_TTL`：可以加进 `HOST_SETTINGS`，复用现有心跳分发；runner 需要改为每次认领时用生效值。

**需要另做设计才能搬的**有三项：`AGENT_RUNTIME_REGISTRY_URL`（控制面和每台 runner 必须指向同一个
注册表）、`AGENT_MODEL_CONNECTIONS_ENABLED`（取决于集群里所有控制面副本是否都认识模型连接）、
`AGENT_API_DOCS`（在构建应用时读取，要先把文档路由改成按请求判断）。

**应当留在部署环境里的**：数据库连接、设置根密钥、恢复用的 `ADMIN_TOKEN`、挂载文件和路径、
发布身份（部署 SHA、镜像来源、构建时间）、监听地址和 Compose 拓扑、主机身份与注册凭据、主机拓扑、
沙箱隔离和沙箱网络边界、性能剖析的部署上限、镜像构建输入。理由是：要么在设置快照可读之前就要用，
要么是部署对自身的陈述，要么一旦可改就能放宽隔离（AGENTS.md §7）。

其余是我们自己注入子进程的内部变量、平台或第三方镜像的变量、仍在兼容期内的旧名，以及只给测试、
CI 和本地开发栈用的变量。

## 2. 本 PR 做什么、不做什么

**做**：

1. 一个 AST 扫描器，识别本仓库实际用到的环境变量读法（§6.1），以及 Compose、示例 env 文件、
   Dockerfile `ARG` 和前端里出现的变量名。
2. 一张分类表：每个变量（或一个变量族的前缀）恰好归到一个去向，并写明理由。
3. 在现有 `make test` 里新增门禁测试，CI 的单元测试分片自动执行，不需要改工作流：
   - 新出现的变量没有归类，测试失败；
   - 分类表里重复、互相覆盖、没有理由、或者已经没有任何地方用到的条目，测试失败；
   - `admin` 条目必须恰好等于 `SETTINGS` 的环境变量名，`host_admin` 条目必须恰好等于
     `HOST_SETTINGS` 的环境变量名加上主机 slots，`retired` 条目必须恰好等于仍在映射的旧名；
   - 后端代码绕过解析器直接读已注册设置，必须在精确的例外表里（模块、函数、变量名三元组）写明理由；
   - 主机设置只能在登记过的引导位置从主机环境读取，控制面不得读主机设置的环境变量；
   - 读不出变量名的读取只能是登记过的注册表循环；把整个环境交给子进程的地方也要逐一登记；
   - 新的带环境变量名的注册必须进 `MIGRATED_SETTINGS`（§5.1）；
   - 内部变量不得出现在运维模板里，工具类变量不得被后端读取或出现在生产模板里；
   - 每一条例外都必须仍然对应一处真实读取，不再需要时测试会要求删除。
4. 扫描器自测和每条规则的反例测试：每种读法都有样例，每条门禁都喂一个违规样本并断言失败。

**不做**：

- 不迁移任何设置，不改 `MIGRATED_SETTINGS`、`HOST_SETTINGS`、默认值或校验规则。
- 不锁定 `control_signing_secret`，不改任何密钥轮换流程（§7.3）。
- 不改 ENVIRONMENT.md 和其他维护中的指南；文档缺口记录在 §8。
- 不涉及 `MODEL_CONNECTIONS_ENABLED` 的混合版本安全、PostgreSQL `application_name`、SQL 触发器、
  部署上限变量、密钥轮换和生产配置。这些在 §7 里只记录前提和不确定性，不作为普通迁移的前置条件。
- 分类表只放在 `tests/support/` 下，不进入任何运行时镜像（各 Dockerfile 只复制 `apps/backend` 下的三个包）。

## 3. 盘点口径与数字

数字由门禁本身的扫描得出（`git ls-files` 加上未被忽略的新文件），可以重现；按名字计数和按读取点计数
分开报告，重叠的表不相加。

| 口径 | 数量 |
|---|---|
| 唯一变量名（含 1 个动态前缀 `AGENT_SECRET_PATTERN_*`） | 333 |
| 其中 Python 读取的唯一名 | 166 |
| 其中模板、构建文件或前端出现的唯一名 | 252 |
| 两边都出现的 | 102 |
| 只存在于注册表里的（没有任何读取点或模板） | 4（四个网页搜索变量，见 §8） |
| Python 读取记录（所有扫描范围） | 315：`raw` 184、`resolver` 88、`copy` 28、`compat` 10、`helper` 5 |
| 其中后端（`apps/backend`）读取记录 | 253，涉及 132 个唯一名 |
| 模板引用记录 | 758：Compose 替换 272、容器 `environment:` 键 258、示例 env 157、Dockerfile `ARG` 39、前端 25、Compose 构建参数 7 |

按去向分（唯一名）：

| 去向 | 数量 | 用户可读的含义 |
|---|---|---|
| `admin` | 64 | 已在 Admin |
| `host_admin` | 24 | 主机设置，经心跳下发 |
| `candidate` | 2 | 可按常规方式迁入 Admin |
| `design` | 3 | 要另做设计才能迁 |
| `deployment` | 135 | 启动、部署、引导输入，留在环境里 |
| `operation` | 1 | 改值是专门的运维流程（根密钥轮换） |
| `internal` | 35 | 我们的代码注入子进程，运维不设 |
| `platform` | 25 | 操作系统、Docker、sshd、AWS SDK、第三方镜像的变量 |
| `retired` | 9 | 兼容期内的旧名 |
| `tooling` | 35 | 测试、CI、基准、本地开发栈 |

分类表本身有 39 个族、291 个精确名和 6 个前缀（`AGENT_TUNNEL_`、`AGENT_MCP_TUNNEL_`、
`AGENT_CONTROL_PLANE_TUNNEL_`、`AGENT_SECRET_PATTERN_`、`PGADMIN_`、`LOCAL_`）。前缀只用在
ENVIRONMENT.md 也按族记录的隧道变量、按前缀扫描的内部变量、第三方 pgAdmin 镜像的命名空间和本地开发栈上；
本地开发栈前缀另有约束：这些名字不得被后端读取，也不得出现在 `deploy/control-plane`、`deploy/host` 的模板里。

逐名的去向和出现位置见附录 A。

## 4. 去向与正交属性

去向只回答「值应当从哪里来」。是否是密钥、是否需要重启、是否被多个进程读取、改了能否撤回，
是另一组属性，写在各条理由里，不再拆成互斥的类别。上一稿的九类在这里合并如下：

- 上一稿的 X（边界参数）、D（部署层）都归 `deployment`。会放宽隔离或扩大可达范围的变量单独成一个族，
  理由里写明「永不成为 admin 设置，改边界是设计变更」。
- 上一稿的 O（操作）只保留真正留在环境里的根密钥 `AGENT_SETTINGS_ENCRYPTION_KEY`。已经注册为设置的
  几把密钥仍是 `admin`，它们「替换即失效」的性质写在理由里。
- 上一稿的 M（双镜像）和 A（集群断言）对应这里的 `design`：今天没有机制保证两边一致或集群事实成立。
- 上一稿的 G（守卫设置）和 S¹（单向设置）是对已注册设置的改进建议，不是去向，本 PR 不实现。

## 5. 迁移约束

### 5.1 只有 `MIGRATED_SETTINGS` 会给已有数据库补导入

`initialize_settings` 在设置行已存在时，只对 `MIGRATED_SETTINGS` 里缺失的键调用
`initial_settings(keys=missing)`；`seed_settings` 用 `EXCLUDED."values" || agent_settings."values"`
合并，已保存的值优先。`_decode` 对缺失的键用默认值。因此：

| 情形 | 环境变量的命运 |
|---|---|
| 全新数据库 | 所有注册名导入一次，之后数据库为准 |
| 已有数据库，新项进 `MIGRATED_SETTINGS` | 下次启动导入一次，之后数据库为准 |
| 已有数据库，新项只进 `SETTINGS` | 永远不导入：升级后直接用默认值，没有日志 |

这不是假设。按 git 历史，下列 8 个名字是在设置行引入（#392）之后才注册、且没有进
`MIGRATED_SETTINGS` 的：`WEB_SEARCH_PROVIDER`、`SERPER_API_KEY`、`BRAVE_SEARCH_API_KEY`、
`PARALLEL_API_KEY`、`AGENT_MCP_GITHUB_CLIENT_ID`、`AGENT_MCP_GITHUB_CLIENT_SECRET`、
`AGENT_WEB_DOCS_URL`、`AGENT_WEB_CONTACT_URL`。在它们注册之前就建好设置行的数据库，
从未导入过这些变量的环境值。是否有部署因此受影响取决于生产配置，本 PR 没有核对。

门禁的做法：把今天 `SETTINGS` 里带环境变量名、但不在 `MIGRATED_SETTINGS` 里的 44 个名字冻结为
`UNMIGRATED_ENV_SETTINGS`。之后任何新的带环境变量名的注册如果不在 `MIGRATED_SETTINGS` 里，测试失败；
冻结表里的名字离开 `SETTINGS` 外的集合（比如移进了 `MIGRATED_SETTINGS`），测试要求同步删除。
这条检查只能保证「新注册走对列表」，不能证明历史上的导入行为，也拦不住有人直接往冻结表里加名字；
那是评审时一眼能看到的改动。

另一个前提：`initial_settings` 遇到不合法的环境值会抛 `ValueError`，控制面因此起不来。给已有变量
加注册时，取值范围必须包住部署里可能存在的旧值，或者先放宽再收紧。

### 5.2 注册之后同名环境变量的其他作用

注册之后，服务进程通过 `setting_env` / `get_setting` 只看数据库快照。但同名环境变量仍可能有别的读者，
门禁把每一处都登记在 `RAW_SETTING_READS` 里：

- `AGENT_SESSION_SECRET`：`domain_cipher.settings_root_key()` 在 `AGENT_SETTINGS_ENCRYPTION_KEY`
  未设置时把它当作设置根密钥。只有在这种回退状态下，改环境里的 `AGENT_SESSION_SECRET` 才会改变根密钥，
  进而让已保存的加密设置无法解密；设置了专用根密钥时，改这个环境变量对根密钥和会话签名都没有作用
  （会话签名以数据库里的值为准）。
- `AGENT_PGADMIN_URL`：`deployment_pgadmin_services()` 直接读环境，与内置的 `http://pgadmin:5050`
  和 `AGENT_PGADMIN_BOOTSTRAP_URL` 一起构成「部署允许的 pgAdmin 服务」。保存的 URL 不在这个集合里时，
  `pgadmin_config()` 返回 503。所以去掉环境里的自定义条目只会让自定义地址不可用，不会让控制面变成
  不受限的转发代理。
- `AGENT_LOG_LEVEL` / `AGENT_LOG_FORMAT`：控制面在第一次采纳快照之前用环境值配置日志，之后由
  `apply_settings` 换成保存值；主机进程把同名变量当作自己的部署配置。
- `AGENT_CLOUDLAB_API_URL`：`ComputeConfiguration.from_env()` 读进一个回退字段；控制面始终以
  `with_runtime_settings()` 运行，请求时读保存值。
- `AGENT_SANDBOX_MANAGER_TOKEN`：控制面那份在数据库里，runner 和 sandbox manager 各读自己的环境。
  托管主机的值是设置主机时从数据库写进它 `.env` 的快照（`compute_setup._assets`），之后不会自动跟随。

不经过 `setting_env` 的一次性工具（`deploy/`、`scripts/` 下的脚本）仍然直接读环境；门禁只对
`apps/backend` 执行解析器规则。

### 5.3 主机设置复用心跳语义

`HOST_SETTINGS` 的优先级是「单台主机的值 > 全局值 > 内置默认值」。控制面在每次心跳应答里下发完整的
生效配置；主机的环境变量只在收到第一次应答之前起作用，并作为 `settings_local` 回报用于诊断；
清除管理员值不会让旧环境值重新生效。主机必须声明 `host-settings-v1` 能力。新的主机设置应当直接加进
`HOST_SETTINGS`，不需要另建分发服务。

门禁把主机从自己环境读取主机设置的位置逐一登记在 `HOST_BOOTSTRAP_READS`。其中一处是已知欠账：
`runner._shared_live_host_capabilities` 校验的是本地的 `AGENT_SHARED_RESIDENT_CAPACITY`，
不看管理员值；本地值越界时，即使管理员值合法，这台主机也不会声明 shared-live 能力。本 PR 不改它。

`AGENT_HOST_SLOTS` 不在 `HOST_SETTINGS` 里，但走的是同一种「管理员值经心跳下发、环境值作默认」的路径
（`PUT /admin/agent/runner-hosts/<host>/slots`），所以也归 `host_admin`，测试里单独列出。

## 6. 门禁

### 6.1 扫描器能识别的读法

- `os.getenv`、`os.environ[...]`、`.get`、`.setdefault`、有返回值的 `.pop`、`in os.environ`，
  以及 `from os import ...`、`import os as ...`、`_get = os.getenv` 这类别名；
- 名字来自字符串常量、常量元组的循环、`map(os.environ.get, NAMES)`、同模块或跨模块导入的常量、
  包 `__init__` 的再导出、`ENV_KEY = OTHER_KEY` 这样的常量别名；
- `manager_env(...)` 记为 `compat`，`setting_env(...)` 记为 `resolver`；
- 映射读取：绑定到 `os.environ`、`dict(os.environ)`、`os.environ.copy()`、`{**os.environ, ...}`、
  `env if env is not None else os.environ`、`manager_environment(os.environ)`（`compat`）或
  `settings_environment()`（`resolver`）的局部变量和 `self` 属性，对它们的 `.get`、`[...]`、`in`；
- 映射传给另一个函数时，按限定名（`模块:类.函数`）找到被调函数，把对应形参也当作映射继续跟踪，
  例如 `build_backend_from_env(env)` → `network_from_env(source)` → `source.get(...)`；
- 把形参直接交给读取的辅助函数（`_env_float(name, ...)`），在同模块里按调用点的实参记为读取；
- 推导式里 `k.startswith("PREFIX")` 的前缀扫描；
- 字符串常量里嵌入的 Python 脚本（远端强制命令、环境脚本辅助程序、快照扫描）；
- 读不出名字的读取如实报告：参数表达式（如 `setting.env`）随结果一起给出；整个环境被交出去的地方记为 `copy`。

非 Python 部分：Compose 的 `${NAME}` 替换、服务的 `environment:` 键和 `build.args`；`.env.example`
的键（含注释掉的 `# NAME=`）；Dockerfile 的 `ARG`；前端非测试源码里的 `process.env.NAME`、
`process.env['NAME']`，以及 `site.ts` 里 `SITE_CONFIG_ENV` 的带引号名字。

### 6.2 看不到的

- 运行时拼出来的名字（`"AGENT_" + suffix`）和调用方传入、源码里看不出与 `os.environ` 有关的映射；
- `getattr(os, "environ")`、`os.environb`，以及第三方库自己读的变量（boto3 的 `AWS_*`、Docker CLI、
  git、ssh），后者只在模板里出现时才被分类；
- 继承、重写和动态分派下的被调函数；
- Shell 脚本（`deploy.sh`、`sandbox_host.sh`、`artifact_env.sh`、`ci-local.sh`、`test-db-local.sh`）：
  里面的 `$NAME` 分不清是环境变量还是局部变量，不纳入门禁；
- Dockerfile 里 `RUN` 的 `${...}`（是 shell 变量），以及 `ENV` 行（那是赋值，不是读取）；
- `tests/` 目录（`TEST_DB_NAME` 等只在测试里读）和 TypeScript 写的沙箱辅助程序（`pi-mcp-bridge.ts`）；
- 只写不读的子进程变量（如运行时适配器设置的 `ANTHROPIC_MODEL`、`CLAUDE_CONFIG_DIR`）：它们不是运维配置面。

### 6.3 例外表

| 表 | 键 | 当前条目 |
|---|---|---|
| `RESOLVER_MODULES` | 模块 | `settings.py`、`sandbox_manager_compat.py` |
| `RAW_SETTING_READS` | （模块，函数，变量） | 11 条，见 §5.2 |
| `HOST_BOOTSTRAP_READS` | （模块，函数） | 10 条，见 §5.3 |
| `REGISTRY_READS` | （模块，函数） | 3 条，都是 `HOST_SETTINGS` 循环 |
| `ENVIRONMENT_COPIES` | （模块，函数） | 8 条：ssh、Docker CLI、git、协议兼容启动器、运行时探测 |
| `UNMIGRATED_ENV_SETTINGS` | 变量 | 44 个，冻结 |

所有例外都是精确键，没有按目录或按模块整体豁免（两个解析器模块除外）；每条都有理由，并由
`test_every_exception_is_still_needed` 检查仍然需要。没有使用 `xfail`。

## 7. 第一批迁移候选

### 7.1 现在就能做

| 变量 | 目标 | 前提 | 验收 |
|---|---|---|---|
| `AGENT_MAX_STREAMS_PER_USER` | `MIGRATED_SETTINGS` 新增整数设置（默认 24） | 门禁已要求进 `MIGRATED_SETTINGS`；`max_streams_per_user()` 改为读设置（它本来就在每次占用时现读）；上限必须包住部署里可能已有的值（§5.1）；描述里写明这是每个进程的上限；更新 ENVIRONMENT.md 这一行及中文翻译 | 已有数据库加 env=40 升级后，数据库值为 40；在 Admin 改成 10，约 5 秒同步后第 11 个流返回 429；之后改环境变量不再生效 |
| `AGENT_LEASE_TTL` | `HOST_SETTINGS` 新增 runner 范围的整数秒设置 | runner 在每次认领时使用生效值，而不是启动时的 argparse 默认值，并把它传给每个回合的子进程；下限大于一次心跳间隔加一次请求超时（今天是 30 + 10 秒），上限不超过控制面的 900 秒；旧 runner 不认识新键，`admin_values` 会丢弃它，继续用环境值，页面按 `settings_local` 显示 | 新 runner 认领请求里的 `lease_ttl_seconds` 等于管理员值；旧 runner 显示仍在用本地值 |

### 7.2 需要另做设计

| 变量 | 为什么不能直接搬 | 设计要回答的问题 |
|---|---|---|
| `AGENT_RUNTIME_REGISTRY_URL` | 控制面用它列版本，每台 runner 用它下载；两边必须相同。`HostSetting` 只支持整数 | 字符串值怎么随心跳下发；主机如何回报正在用的注册表；切换时先让主机采纳、再让控制面使用 |
| `AGENT_MODEL_CONNECTIONS_ENABLED` | 经 `setting_env` 读取但未注册，每个副本看自己的环境；安全开启依赖「集群里没有不认识模型连接的旧副本」这个事实，目前没有任何地方记录 | 能证明这个事实的证据从哪里来。混合版本安全、`application_name`、SQL 触发器都属于这个问题，本 PR 范围外，也不是其他普通迁移的前提 |
| `AGENT_API_DOCS` | `create_app()` 构建路由时读取，此时没有设置快照 | 文档路由能否改成按请求判断 |

### 7.3 已注册设置上的其他提议（不在本 PR）

- `control_signing_secret` 今天可以在 Admin 里替换，描述明确写了「替换会使现有主机和 worker 凭据失效，
  恢复工作前要重新注册主机」。是否改成锁定或双密钥轮换是设计选择，要有证据再定；本 PR 不改。
- `AGENT_SANDBOX_MANAGER_TOKEN` 的轮换（双令牌窗口、托管主机重新渲染 `.env`）属于密钥轮换，范围外。
- `AGENT_EGRESS_PROTECTED_CIDRS` 由主机执行，控制面那份只用于 SSH 服务器检查和模型连接校验的拨号；
  它属于沙箱网络边界，留在部署环境。是否改为从主机回报推导，是边界设计问题。
- `AGENT_TRUST_PROXY_HEADERS`、`AGENT_SESSION_COOKIE_SECURE`、`AGENT_DEFAULT_EXECUTION_PROFILE`
  这类可能需要部署上限或写入/读取双侧校验的设置，维持现状，另行评估。

## 8. 对上一稿的修正

1. **性质**：上一稿只是文档和门禁草案，没有实现 CI；本 PR 才实现。UI 方面的后续工作已在 #1034 实现
   （设置页简化、移除 Deployment 卡片），与本 PR 无关，本 PR 不改前端。
2. **计数**：上一稿把「64 项设置」「38 项未注册」「74 个漏掉的读取点」放在一起；后者混合了名字和读取点。
   本文按唯一名和读取记录分开计数（§3）。`SETTINGS` 116 项、其中 64 项带环境变量名、`HOST_SETTINGS` 23 项，
   这几个数复核无误；ENVIRONMENT.md 表格首列共 219 个记号，也与上一稿一致。
3. **pgAdmin**：保存的 upstream 不在部署允许的服务里时返回 503（§5.2），去掉自定义允许项不会变成开放代理。
4. **`control_signing_secret`**：上一稿把「不在锁定集合里」列为缺口并建议锁定；它的可替换性和后果在描述里写明了，
   是否收紧是设计选择（§7.3）。
5. **`AGENT_SESSION_SECRET`**：上一稿说按旧手册改环境变量会让「控制面起不来」，这只在没有专用根密钥、
   正在使用回退时成立（§5.2）。
6. **「只进 `SETTINGS` 的新注册不会导入」**：上一稿是推断，本文用代码和 git 历史确认，并找到 8 个实例（§5.1）。
7. **分类表的位置**：上一稿建议放进 `cloud_agent_shared` 随两个镜像发布；本 PR 放在 `tests/support/`，不进镜像。
8. **迁入方式**：上一稿建议用 `xfail(strict=True)` 挂起已知违规；本 PR 改用精确例外表加「例外仍被需要」检查，
   不挂起任何门禁。
9. **几项归类**：`AGENT_GATEWAY_SSH_HOST` 不需要退役，它和 `AGENT_CONTROL_PLANE_SSH_HOST` 一样只是表单预填值，
   保存的请求优先；`AGENT_TRUST_PROXY_HEADERS` 维持已注册状态，不做反向迁移；`AGENT_ACTION_UID` 只被写入，
   没有 Python 读者，只在文档里出现。
10. **上一稿的 G11（数据库会话打标签）和 §5.3 的 P1–P6**：范围外，只作为 `MODEL_CONNECTIONS_ENABLED`
    设计的前提记录（§7.2），不是其他迁移的前提。

## 9. 发现的缺口与未决问题

- **网页搜索的四个变量**（`WEB_SEARCH_PROVIDER`、`SERPER_API_KEY`、`BRAVE_SEARCH_API_KEY`、
  `PARALLEL_API_KEY`）已经注册，但 ENVIRONMENT.md 没有对应行，受支持的控制面 Compose 也不传它们。
  按受支持的 Compose 部署时，首次导入只能通过 override 文件发生；它们又不在 `MIGRATED_SETTINGS` 里。
  现有的 `test_environment_inventory.py` 只检查 `AGENT_`/`GATEWAY_`/`IDENTITY_` 前缀，所以没有发现。
  建议的后续：在 Admin 里设置这四项，ENVIRONMENT.md 补行并说明「首次导入，之后在 Admin 修改」。
- **只在文档里出现的名字**（17 个）：已退役的 `AGENT_EGRESS_*`、`AGENT_SANDBOX_NETWORK`、
  `AGENT_SANDBOX_ALLOW_OPEN_NETWORK`、`AGENT_MCP_CONFIG`；`sandbox_host.sh` 会提醒的
  `AGENT_RUNNER_REPLICAS`；Gateway 侧的 `IDENTITY_*`；只在 shell 或测试里读的 `CI_LOG_DIR`、
  `TEST_DB_NAME`；只被写入的 `AGENT_ACTION_UID`。门禁只管被读取或在模板里出现的名字，这些不进分类表。
- **主机能力检查用本地 `AGENT_SHARED_RESIDENT_CAPACITY`**（§5.3）：已登记为欠账。
- **托管主机的 sandbox manager 令牌是设置时的快照**（§5.2）：轮换设计范围外。
- **部署实际取值**：哪些部署设置过 §5.1 的 8 个变量、生产库里设置行建于何时，本 PR 没有查看生产配置。
- **ENVIRONMENT.md「Adding a variable」一节**还没有提到分类表。门禁失败信息会直接指出要改哪个文件；
  是否把这一步写进指南（需要同步中文翻译），留给维护者决定。

## 附录 A：逐名去向

由门禁扫描生成（基线 `4b7b826d` 加本 PR）。「出现位置」里 `py` 后面是读取它的包（`cp` 控制面、`host`、
`shared`、`sandbox-helper` 沙箱镜像里的 Python 辅助程序、`deploy-py`、`scripts`、`migrations`）和读法；
其余是模板类型和所在部署目录（`cp` 为 `deploy/control-plane`，`host` 为 `deploy/host`，`local` 为本地开发栈，
`sandbox` 为沙箱镜像，`web` 为前端）。「仅注册表」表示只在 `SETTINGS` 里出现。

| 名称 | 去向 | 出现位置 |
|---|---|---|
| `AGENT_ALERT_WEBHOOK_URL` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_CLOUDLAB_API_URL` | admin | compose cp; container cp; py cp/raw |
| `AGENT_COMPUTE_REGISTRATION_URL` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_COMPUTE_SECRET_KEY` | admin | compose cp; container cp; example cp; py cp/resolver |
| `AGENT_CONTROL_TOKEN_SECRET` | admin | compose cp; container cp; example local; py cp/resolver |
| `AGENT_DEFAULT_EXECUTION_PROFILE` | admin | compose cp; container cp; container local; py cp/resolver |
| `AGENT_DEFAULT_MODEL` | admin | compose cp; container cp |
| `AGENT_GITHUB_API` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_GITHUB_APP_CLIENT_ID` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_GITHUB_APP_CLIENT_SECRET` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_GITHUB_APP_ID` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_GITHUB_APP_INSTALL_URL` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_GITHUB_APP_PRIVATE_KEY` | admin | py cp/resolver |
| `AGENT_GITHUB_TOKEN` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_GITLAB_OAUTH_CLIENT_ID` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_GITLAB_OAUTH_CLIENT_SECRET` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_GITLAB_OAUTH_REDIRECT_URI` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_GIT_AUTHOR_EMAIL` | admin | compose cp; compose host; container cp; container host; example host; py cp/resolver |
| `AGENT_GIT_AUTHOR_NAME` | admin | compose cp; compose host; container cp; container host; example host; py cp/resolver |
| `AGENT_LOG_FORMAT` | admin | compose cp; compose host; container cp; container host; py cp/raw; py host/raw |
| `AGENT_LOG_LEVEL` | admin | compose cp; compose host; container cp; container host; py cp/raw; py host/raw |
| `AGENT_MCP_ALLOW_PLAIN_HTTP` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_MCP_ALLOW_PRIVATE_TARGETS` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_MCP_GITHUB_CLIENT_ID` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_MCP_GITHUB_CLIENT_SECRET` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_MCP_PUBLIC_BASE_URL` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_ONCALL_DISPATCH_TOKEN` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_ONCALL_USER_ID` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_PGADMIN_PUBLIC_URL` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_PGADMIN_URL` | admin | compose cp; container cp; py cp/raw; py cp/resolver |
| `AGENT_PUBLISH_BASE_BRANCH` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_REPO_ALLOWLIST` | admin | compose cp; container cp; example local; py cp/resolver |
| `AGENT_RESIDENCY_TTL_S` | admin | compose cp; container cp |
| `AGENT_SANDBOX_ARCHIVE_S3_BUCKET` | admin | compose cp; container cp; example cp |
| `AGENT_SANDBOX_ARCHIVE_S3_ENDPOINT` | admin | compose cp; container cp; example cp |
| `AGENT_SANDBOX_ARCHIVE_S3_PREFIX` | admin | compose cp; container cp; example cp |
| `AGENT_SANDBOX_ARCHIVE_S3_REGION` | admin | compose cp; container cp; example cp |
| `AGENT_SANDBOX_MANAGER_TOKEN` | admin | compose cp; compose host; compose local; container cp; container host; container local; example cp; example host; example local; py cp/resolver; py host/compat |
| `AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS` | admin | compose cp; container cp; example local; py cp/resolver |
| `AGENT_SESSION_COOKIE_SECURE` | admin | compose cp; container cp; example local; py cp/resolver |
| `AGENT_SESSION_REDIRECT_URI` | admin | compose cp; container cp; example local; py cp/resolver |
| `AGENT_SESSION_SECRET` | admin | compose cp; container cp; example local; py cp/raw; py cp/resolver |
| `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` | admin | compose cp; container cp; example local; py cp/resolver |
| `AGENT_TRUST_PROXY_HEADERS` | admin | compose cp; container cp; py cp/resolver |
| `AGENT_WEB_APP_NAME` | admin | compose cp; container cp; example local; web web |
| `AGENT_WEB_BASE_URL` | admin | compose cp; container cp; example local; py cp/resolver |
| `AGENT_WEB_CONTACT_URL` | admin | compose cp; container cp; web web |
| `AGENT_WEB_DOCS_URL` | admin | compose cp; container cp; web web |
| `AGENT_WEB_FEATURE_RAG` | admin | compose cp; container cp; web web |
| `AGENT_WEB_GATEWAY_URL` | admin | compose cp; container cp; example local; web web |
| `AGENT_WEB_ORG_NAME` | admin | compose cp; container cp; web web |
| `AGENT_WEB_ORG_URL` | admin | compose cp; container cp; web web |
| `AGENT_WEB_STATUS_URL` | admin | compose cp; container cp; web web |
| `BRAVE_SEARCH_API_KEY` | admin | 仅注册表 |
| `GATEWAY_AUTHORIZE_URL` | admin | compose cp; container cp; example local; py cp/resolver |
| `GATEWAY_BASE_URL` | admin | compose cp; container cp; example local; py cp/resolver |
| `GATEWAY_GRANT_BASE_PATH` | admin | compose cp; container cp; py cp/resolver |
| `GATEWAY_GRANT_BINDING_ID` | admin | compose cp; container cp; py cp/resolver |
| `GATEWAY_GRANT_DISPATCH_TOKEN` | admin | compose cp; compose local; container cp; example local; py cp/resolver; py deploy-py/raw |
| `GATEWAY_ISSUER` | admin | compose cp; container cp; py cp/resolver |
| `GATEWAY_JWKS_URL` | admin | compose cp; container cp; example local; py cp/resolver |
| `PARALLEL_API_KEY` | admin | 仅注册表 |
| `SERPER_API_KEY` | admin | 仅注册表 |
| `WEB_SEARCH_PROVIDER` | admin | 仅注册表 |
| `AGENT_CHAT_IDLE_TIMEOUT_S` | host_admin | compose host; container host; container local; example host; py host/raw |
| `AGENT_HOST_SLOTS` | host_admin | compose host; container host; container local; example cp; example host; py host/raw |
| `AGENT_SANDBOX_CPU_MILLICORES` | host_admin | compose host; container host; example host; py host/raw |
| `AGENT_SANDBOX_DISK_MIB` | host_admin | compose host; container host; example host; py host/raw |
| `AGENT_SANDBOX_MEMORY_MIB` | host_admin | compose host; container host; example host; py host/raw |
| `AGENT_SHARED_RESIDENT_CAPACITY` | host_admin | compose host; container host; container local; example cp; example host; py host/raw |
| `AGENT_SOURCE_IMPORT_MAX_MIB` | host_admin | compose host; container host; example host; py host/raw |
| `AGENT_STORAGE_MIN_AVAILABLE_MIB` | host_admin | py host/raw |
| `AGENT_TERMINAL_BUFFER_MAX_EVENTS` | host_admin | py host/raw |
| `AGENT_TERMINAL_BUFFER_MAX_MIB` | host_admin | py host/raw |
| `AGENT_TERMINAL_HARD_TTL_SECONDS` | host_admin | py host/raw |
| `AGENT_TERMINAL_IDLE_TTL_SECONDS` | host_admin | py host/raw |
| `AGENT_TERMINAL_INPUT_MAX_KIB` | host_admin | py host/raw |
| `AGENT_TERMINAL_MAX_SESSIONS` | host_admin | py host/raw |
| `AGENT_TERMINAL_MAX_SESSIONS_PER_WORKSPACE` | host_admin | py host/raw |
| `AGENT_TERMINAL_MAX_STREAMS_PER_SESSION` | host_admin | py host/raw |
| `AGENT_TERMINAL_OUTPUT_MAX_MIB` | host_admin | py host/raw |
| `AGENT_TIMEOUT_S` | host_admin | compose host; container host; container local; py host/raw |
| `AGENT_WORKSPACE_COMMAND_OUTPUT_MAX_MIB` | host_admin | py host/raw |
| `AGENT_WORKSPACE_COMMAND_TIMEOUT_S` | host_admin | py host/raw |
| `AGENT_WORKSPACE_DIRECTORY_MAX_ENTRIES` | host_admin | py host/raw |
| `AGENT_WORKSPACE_DOWNLOAD_MAX_MIB` | host_admin | py host/raw |
| `AGENT_WORKSPACE_FILE_PREVIEW_MAX_KIB` | host_admin | py host/raw |
| `AGENT_WORKSPACE_FILE_WRITE_MAX_MIB` | host_admin | py host/raw |
| `AGENT_LEASE_TTL` | candidate | compose host; container host; container local; py host/raw |
| `AGENT_MAX_STREAMS_PER_USER` | candidate | compose cp; container cp; py cp/raw |
| `AGENT_API_DOCS` | design | compose cp; container cp; py cp/raw |
| `AGENT_MODEL_CONNECTIONS_ENABLED` | design | compose cp; container cp; py cp/resolver |
| `AGENT_RUNTIME_REGISTRY_URL` | design | compose cp; compose host; container cp; container host; example host; py cp/raw; py host/raw |
| `ADMIN_TOKEN` | deployment | compose cp; container cp; example cp; py cp/raw |
| `AGENT_COMPUTE_CONFIG` | deployment | compose cp; container cp; example cp; py cp/raw |
| `AGENT_COMPUTE_DIRECTORY` | deployment | compose cp; example cp |
| `AGENT_COMPUTE_SANDBOX_IMAGE_REPOSITORY` | deployment | compose cp; container cp; py cp/raw |
| `AGENT_CONTAINERD_CONTENT_DIR` | deployment | container host; py host/raw |
| `AGENT_CONTROL_PLANE_BIND` | deployment | compose cp |
| `AGENT_CONTROL_PLANE_COMPOSE_OVERRIDE_FILE` | deployment | example cp |
| `AGENT_CONTROL_PLANE_HOSTNAME` | deployment | compose host |
| `AGENT_CONTROL_PLANE_PORT` | deployment | compose cp; example local |
| `AGENT_CONTROL_PLANE_SSH_HOST` | deployment | compose cp; container cp; example cp; py cp/raw |
| `AGENT_CONTROL_PLANE_TUNNEL_LISTEN_PORT` | deployment | compose host |
| `AGENT_CONTROL_PLANE_TUNNEL_REMOTE_HOST` | deployment | compose host; example cp |
| `AGENT_CONTROL_PLANE_TUNNEL_REMOTE_PORT` | deployment | compose host; example cp |
| `AGENT_CONTROL_PLANE_TUNNEL_SSH_DESTINATION` | deployment | compose host; example cp; example host |
| `AGENT_CONTROL_PLANE_TUNNEL_SSH_KEY` | deployment | compose host; example cp; example host |
| `AGENT_CONTROL_PLANE_TUNNEL_SSH_KNOWN_HOSTS` | deployment | compose host; example cp; example host |
| `AGENT_CONTROL_PLANE_TUNNEL_SSH_PORT` | deployment | compose host |
| `AGENT_CONTROL_PLANE_URL` | deployment | compose host; container cp; container host; container local; py host/raw; web web |
| `AGENT_DATABASE_URL` | deployment | compose cp; container cp; example cp; example local; py cp/raw; py migrations/raw |
| `AGENT_DEPLOYMENT_SHA` | deployment | compose cp; compose host; compose local; container cp; container host; container local; example cp; example local; py cp/raw; py host/raw; web web |
| `AGENT_EDGE_BIND` | deployment | compose cp; example cp |
| `AGENT_EDGE_CONFIG_FILE` | deployment | compose cp |
| `AGENT_EDGE_PORT` | deployment | compose cp; example cp |
| `AGENT_EGRESS_CHAT_NETWORK_POOL` | deployment | compose host; container host; container local; example cp; example host; py shared/raw |
| `AGENT_EGRESS_DNS_SERVER` | deployment | compose host; container host; container local; example cp; example host; py shared/raw |
| `AGENT_EGRESS_PROTECTED_CIDRS` | deployment | compose cp; compose host; container cp; container host; container local; example cp; example host; py cp/raw; py shared/raw |
| `AGENT_ENROLL_TOKEN` | deployment | compose host; container host; container local; example host; py host/raw |
| `AGENT_GATEWAY_HOSTNAME` | deployment | compose host |
| `AGENT_GATEWAY_SSH_HOST` | deployment | compose cp; container cp; example cp; py cp/raw |
| `AGENT_GATEWAY_URL` | deployment | container host; container local; py host/raw; py shared/raw |
| `AGENT_GITHUB_APP_PRIVATE_KEY_FILE` | deployment | compose cp; example cp |
| `AGENT_GITHUB_APP_PRIVATE_KEY_PATH` | deployment | compose cp; container cp; container local; py cp/raw; py cp/resolver |
| `AGENT_HOST_CAPACITY_ENABLED` | deployment | compose host; container host; example host; py host/raw |
| `AGENT_HOST_CONTAINERD_CONTENT_DIR` | deployment | compose host; example host |
| `AGENT_HOST_CREDENTIAL` | deployment | py host/raw |
| `AGENT_HOST_CREDENTIAL_FILE` | deployment | container host; container local; py host/raw |
| `AGENT_HOST_STATE_ROOT` | deployment | compose host; example host |
| `AGENT_HOST_STORAGE_PROBE` | deployment | compose host; container host; example cp; example host; py host/raw |
| `AGENT_MCP_HOSTNAME` | deployment | compose host |
| `AGENT_MCP_RELAY_BIND` | deployment | compose cp |
| `AGENT_MCP_RELAY_PORT` | deployment | compose cp; example local |
| `AGENT_MCP_TUNNEL_LISTEN_PORT` | deployment | compose host |
| `AGENT_MCP_TUNNEL_REMOTE_HOST` | deployment | compose host; example cp |
| `AGENT_MCP_TUNNEL_REMOTE_PORT` | deployment | compose host; example cp |
| `AGENT_MCP_TUNNEL_SSH_DESTINATION` | deployment | compose host; example cp; example host |
| `AGENT_MCP_TUNNEL_SSH_KEY` | deployment | compose host; example cp; example host |
| `AGENT_MCP_TUNNEL_SSH_KNOWN_HOSTS` | deployment | compose host; example cp; example host |
| `AGENT_MCP_TUNNEL_SSH_PORT` | deployment | compose host |
| `AGENT_NETWORK_NAME` | deployment | compose cp; example cp; example local |
| `AGENT_OPERATION_PROFILING_DEPLOYMENT` | deployment | compose cp; compose local; container cp; container local; example cp; py cp/raw |
| `AGENT_OPERATION_PROFILING_ENABLED` | deployment | compose cp; compose host; compose local; container cp; container host; container local; example cp; example host; py shared/raw |
| `AGENT_OPERATION_PROFILING_MODE` | deployment | compose cp; compose host; compose local; container cp; container host; container local; example cp; example host; py shared/raw |
| `AGENT_OPERATION_PROFILING_RUN` | deployment | compose cp; compose host; compose local; container cp; container host; container local; example cp; example host; py shared/raw |
| `AGENT_OPERATION_PROFILING_SINK` | deployment | compose cp; compose host; compose local; container cp; container host; container local; example cp; example host; py host/raw; py shared/raw |
| `AGENT_PGADMIN_BOOTSTRAP_DIR` | deployment | compose cp; container cp; example cp; py cp/raw |
| `AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL` | deployment | compose cp; container cp; example cp; py cp/raw |
| `AGENT_PGADMIN_BOOTSTRAP_URL` | deployment | compose cp; container cp; example cp; py cp/raw |
| `AGENT_PGADMIN_COOKIE_SECURE` | deployment | compose cp; example cp |
| `AGENT_PGADMIN_ENTRYPOINT_FILE` | deployment | compose cp; example cp |
| `AGENT_PGADMIN_SERVERS_FILE` | deployment | compose cp; example cp |
| `AGENT_PHYSICAL_HOST` | deployment | compose host; container host; example host; py host/raw |
| `AGENT_POSTGRES_DB` | deployment | compose cp; compose local; example local |
| `AGENT_POSTGRES_PASSWORD` | deployment | compose cp; compose local; example cp; example local |
| `AGENT_POSTGRES_USER` | deployment | compose cp; compose local; example local |
| `AGENT_RELAY_NETWORK` | deployment | compose host; container host; container local; py shared/raw |
| `AGENT_REPO_CLONE_BASE_URL` | deployment | container local; py host/raw |
| `AGENT_RETAINED_DOCKER_DATA_ROOT` | deployment | compose host; compose local; container host; container local; example cp; example host; example local; py host/raw |
| `AGENT_RUNNER_HOST` | deployment | compose host; container host; container local; example host; py host/raw |
| `AGENT_RUNNER_STOP_GRACE` | deployment | compose host; example host |
| `AGENT_RUNNER_VERSION` | deployment | py host/raw |
| `AGENT_RUNTIME_DOWNLOAD_NETWORK` | deployment | compose host; container host; example host; py host/raw |
| `AGENT_SANDBOX_ALLOW_UNISOLATED` | deployment | py host/raw |
| `AGENT_SANDBOX_ARCHIVE_STATE_DIR` | deployment | container host; py host/raw |
| `AGENT_SANDBOX_BACKEND` | deployment | compose host; container host; container local; example host; py host/raw |
| `AGENT_SANDBOX_DOCKER` | deployment | py host/raw |
| `AGENT_SANDBOX_EXTRA_ARGS` | deployment | py host/raw |
| `AGENT_SANDBOX_GID` | deployment | py host/raw |
| `AGENT_SANDBOX_HOME` | deployment | py host/raw |
| `AGENT_SANDBOX_IMAGE` | deployment | compose host; container host; container local; example cp; example host; py host/raw |
| `AGENT_SANDBOX_IMAGE_PULL` | deployment | example cp |
| `AGENT_SANDBOX_MANAGER_ADVERTISE_URL` | deployment | compose host; container host; container local; example cp; example host; py host/compat |
| `AGENT_SANDBOX_MANAGER_REVERSE_BIND` | deployment | compose host; example cp; example host |
| `AGENT_SANDBOX_MANAGER_REVERSE_PORT` | deployment | compose host; example cp; example host |
| `AGENT_SANDBOX_MANAGER_REVERSE_TARGET_HOST` | deployment | compose host |
| `AGENT_SANDBOX_MANAGER_REVERSE_TARGET_PORT` | deployment | compose host |
| `AGENT_SANDBOX_MANAGER_STOP_GRACE` | deployment | compose host |
| `AGENT_SANDBOX_MANAGER_URL` | deployment | container host; container local; py host/compat |
| `AGENT_SANDBOX_MCP_URL` | deployment | container host; container local; py host/raw; py shared/raw |
| `AGENT_SANDBOX_NAMESPACE` | deployment | container host; container local; py host/raw; py shared/raw |
| `AGENT_SANDBOX_RUNTIME` | deployment | py host/raw |
| `AGENT_SANDBOX_UID` | deployment | py host/raw |
| `AGENT_SHARED_LIVE_READY_FILE` | deployment | container host; container local; py host/raw |
| `AGENT_TUNNEL_LISTEN_PORT` | deployment | compose host |
| `AGENT_TUNNEL_REMOTE_HOST` | deployment | compose host; example cp |
| `AGENT_TUNNEL_REMOTE_PORT` | deployment | compose host; example cp |
| `AGENT_TUNNEL_SSH_DESTINATION` | deployment | compose host; example cp; example host |
| `AGENT_TUNNEL_SSH_KEY` | deployment | compose host; example cp; example host |
| `AGENT_TUNNEL_SSH_KNOWN_HOSTS` | deployment | compose host; example cp; example host |
| `AGENT_TUNNEL_SSH_PORT` | deployment | compose host |
| `AGENT_UPLOAD_RECEIPT_DIR` | deployment | container host; container local; py host/raw |
| `AGENT_WEB_API_URL` | deployment | compose cp; example cp; example local |
| `AGENT_WEB_BASE_PATH` | deployment | compose cp; example cp; example local |
| `AGENT_WEB_BIND` | deployment | compose cp |
| `AGENT_WEB_DEPLOYED_AT` | deployment | compose cp; container cp; web web |
| `AGENT_WEB_PORT` | deployment | compose cp; example local |
| `AGENT_WORKER_ID` | deployment | container local; py host/raw |
| `BUILD_SHA` | deployment | compose cp |
| `BUILD_TIMESTAMP` | deployment | compose cp |
| `CLAUDE_CODE_EXTRA_VERSIONS` | deployment | dockerfile_arg sandbox; example host |
| `CLAUDE_CODE_VERSION` | deployment | dockerfile_arg sandbox |
| `CLAUDE_CODE_VERSIONS` | deployment | dockerfile_arg sandbox |
| `CODEX_EXTRA_VERSIONS` | deployment | dockerfile_arg sandbox; example host |
| `CODEX_VERSION` | deployment | dockerfile_arg sandbox |
| `CODEX_VERSIONS` | deployment | dockerfile_arg sandbox |
| `COMPOSE_PROJECT` | deployment | compose host; example host |
| `DEBIAN_MIRROR` | deployment | dockerfile_arg cp; dockerfile_arg host; dockerfile_arg local; dockerfile_arg sandbox |
| `KILO_EXTRA_VERSIONS` | deployment | dockerfile_arg sandbox; example host |
| `KILO_VERSION` | deployment | dockerfile_arg sandbox |
| `KILO_VERSIONS` | deployment | dockerfile_arg sandbox |
| `NEXT_PUBLIC_AGENT_API_URL` | deployment | build_arg cp; dockerfile_arg cp; web web |
| `NEXT_PUBLIC_BASE_PATH` | deployment | build_arg cp; dockerfile_arg cp; web web |
| `NEXT_PUBLIC_BUILD_SHA` | deployment | build_arg cp; dockerfile_arg cp; web web |
| `NEXT_PUBLIC_BUILD_TIMESTAMP` | deployment | build_arg cp; dockerfile_arg cp; web web |
| `OPENCODE_EXTRA_VERSIONS` | deployment | dockerfile_arg sandbox; example host |
| `OPENCODE_VERSION` | deployment | dockerfile_arg sandbox |
| `OPENCODE_VERSIONS` | deployment | dockerfile_arg sandbox |
| `PIP_DEFAULT_TIMEOUT` | deployment | dockerfile_arg cp; dockerfile_arg host; dockerfile_arg local; dockerfile_arg sandbox |
| `PI_EXTRA_VERSIONS` | deployment | dockerfile_arg sandbox; example host |
| `PI_VERSION` | deployment | dockerfile_arg sandbox |
| `PI_VERSIONS` | deployment | dockerfile_arg sandbox |
| `SHARED_READY_TIMEOUT` | deployment | example host |
| `SIBLING_COMPOSE_PROJECTS` | deployment | example host |
| `SOURCE_COMMIT` | deployment | build_arg cp; dockerfile_arg cp; dockerfile_arg host; dockerfile_arg sandbox |
| `UV_HTTP_TIMEOUT` | deployment | dockerfile_arg cp; dockerfile_arg host |
| `UV_VERSION` | deployment | dockerfile_arg cp; dockerfile_arg host |
| `AGENT_SETTINGS_ENCRYPTION_KEY` | operation | compose cp; container cp; example cp; py cp/raw |
| `AGENT_COMPAT_CLIENT` | internal | py host/raw |
| `AGENT_COMPAT_MODEL` | internal | py host/raw |
| `AGENT_COMPAT_TOKEN` | internal | py host/raw |
| `AGENT_COMPAT_UPSTREAM_KEY` | internal | py host/raw |
| `AGENT_COMPAT_UPSTREAM_URL` | internal | py host/raw |
| `AGENT_ENVIRONMENT_SCRIPT` | internal | py host(embedded)/raw |
| `AGENT_LIVE_SESSION_ID` | internal | py sandbox-helper/raw |
| `AGENT_LIVE_WORKDIR` | internal | py sandbox-helper/raw |
| `AGENT_MCP_SERVERS` | internal | py sandbox-helper/raw |
| `AGENT_MCP_TOKEN` | internal | py sandbox-helper/raw |
| `AGENT_MEMBER_PROCESS_REAPER_SOURCE` | internal | py host/raw |
| `AGENT_PLUGIN_PATHS` | internal | py sandbox-helper/raw |
| `AGENT_SCRIPT_STATUS` | internal | py host(embedded)/raw |
| `AGENT_SECRET_PATTERN_*` | internal | py host(embedded)/raw |
| `KILO_FREEINFERENCE_CONFIG_DIR` | internal | py sandbox-helper/raw |
| `KILO_GATEWAY_MODEL` | internal | py sandbox-helper/raw |
| `KILO_GATEWAY_REASONING_EFFORT` | internal | py sandbox-helper/raw |
| `OPENAI_API_KEY` | internal | py sandbox-helper/raw |
| `OPENAI_BASE_URL` | internal | py sandbox-helper/raw |
| `OPENCODE_FREEINFERENCE_CONFIG_DIR` | internal | py sandbox-helper/raw |
| `OPENCODE_GATEWAY_MODEL` | internal | py sandbox-helper/raw |
| `OPENCODE_GATEWAY_REASONING_EFFORT` | internal | py sandbox-helper/raw |
| `PI_CODING_AGENT_DIR` | internal | py sandbox-helper/raw |
| `PI_GATEWAY_MODEL` | internal | py sandbox-helper/raw |
| `PI_GATEWAY_REASONING` | internal | py sandbox-helper/raw |
| `TUNNEL_LISTEN_PORT` | internal | container host |
| `TUNNEL_REMOTE_HOST` | internal | container host |
| `TUNNEL_REMOTE_PORT` | internal | container host |
| `TUNNEL_REVERSE_BIND` | internal | container host |
| `TUNNEL_REVERSE_PORT` | internal | container host |
| `TUNNEL_REVERSE_TARGET_HOST` | internal | container host |
| `TUNNEL_REVERSE_TARGET_PORT` | internal | container host |
| `TUNNEL_SSH_DESTINATION` | internal | container host |
| `TUNNEL_SSH_PORT` | internal | container host |
| `_KATA_RUNTIME_EPOCH_LOCK_FD` | internal | py deploy-py/raw |
| `AWS_ACCESS_KEY_ID` | platform | example cp |
| `AWS_DEFAULT_REGION` | platform | example cp |
| `AWS_SECRET_ACCESS_KEY` | platform | example cp |
| `AWS_SESSION_TOKEN` | platform | example cp |
| `COMPOSE_PROFILES` | platform | example cp |
| `DOCKER_CONFIG` | platform | py host/raw |
| `DOCKER_CONTEXT` | platform | py host/raw |
| `DOCKER_HOST` | platform | py host/raw |
| `HOME` | platform | py host/raw |
| `HOSTNAME` | platform | py host/raw |
| `LANG` | platform | py host/raw |
| `LC_ALL` | platform | py host/raw |
| `NODE_ENV` | platform | web web |
| `PATH` | platform | py cp/raw; py host/raw |
| `PGADMIN_CONFIG_PROXY_X_PROTO_COUNT` | platform | container cp |
| `PGADMIN_CONFIG_SERVER_MODE` | platform | container cp |
| `PGADMIN_CONFIG_SESSION_COOKIE_SECURE` | platform | container cp |
| `PGADMIN_DISABLE_POSTFIX` | platform | container cp |
| `PGADMIN_LISTEN_PORT` | platform | container cp |
| `POSTGRES_DB` | platform | container cp |
| `POSTGRES_PASSWORD` | platform | container cp |
| `POSTGRES_USER` | platform | container cp |
| `SSH_ORIGINAL_COMMAND` | platform | py cp(embedded)/raw |
| `TERM` | platform | py host/raw |
| `TMPDIR` | platform | py host/raw |
| `AGENT_BROKER_REVERSE_BIND` | retired | compose host |
| `AGENT_BROKER_REVERSE_PORT` | retired | compose host |
| `AGENT_BROKER_REVERSE_TARGET_HOST` | retired | compose host |
| `AGENT_BROKER_REVERSE_TARGET_PORT` | retired | compose host |
| `AGENT_BROKER_STOP_GRACE` | retired | compose host |
| `AGENT_BROKER_URL_ALLOWED_HOSTS` | retired | compose cp |
| `AGENT_WORKSPACE_BROKER_ADVERTISE_URL` | retired | compose host |
| `AGENT_WORKSPACE_BROKER_TOKEN` | retired | compose cp |
| `AGENT_WORKSPACE_BROKER_URL` | retired | 仅注册表 |
| `CAE_BENCH_DNS_SERVER` | tooling | py scripts/raw |
| `CI` | tooling | web web |
| `CLOUD_AGENT_E2E_PORT` | tooling | web web |
| `CLOUD_AGENT_E2E_SKIP_BUILD` | tooling | web web |
| `CLOUD_AGENT_TEST_API_KEY_FILE` | tooling | py deploy-py/raw |
| `CLOUD_AGENT_TEST_PROXY` | tooling | py deploy-py/raw |
| `GITHUB_API_URL` | tooling | py deploy-py/raw |
| `GITHUB_REPOSITORY` | tooling | py deploy-py/raw |
| `GITHUB_RUN_ATTEMPT` | tooling | py deploy-py/raw |
| `GITHUB_RUN_ID` | tooling | py deploy-py/raw |
| `GITHUB_TOKEN` | tooling | py deploy-py/raw |
| `LEGACY_COPY_DATABASE_URL` | tooling | py scripts/raw |
| `LOCAL_AGENT_TIMEOUT_S` | tooling | compose local; example local |
| `LOCAL_CHAT_IDLE_TIMEOUT_S` | tooling | compose local; example local |
| `LOCAL_CHAT_NETWORK_POOL` | tooling | compose local; example local |
| `LOCAL_EDGE_PORT` | tooling | compose local; container local; example local; py deploy-py/raw |
| `LOCAL_EGRESS_DNS_SERVER` | tooling | compose local; example local |
| `LOCAL_EGRESS_PROTECTED_CIDRS` | tooling | compose local; example local |
| `LOCAL_ENROLL_TOKEN` | tooling | compose local; example local |
| `LOCAL_ENROLL_TOKEN_SHA256` | tooling | compose local; container local; example local |
| `LOCAL_GATEWAY_DISPATCH_TOKEN` | tooling | container local; py deploy-py/raw |
| `LOCAL_HOST_SLOTS` | tooling | compose local; example local |
| `LOCAL_IDENTITY_ALLOWED_REDIRECTS` | tooling | compose local; container local; example local; py deploy-py/raw |
| `LOCAL_PROJECT_NAME` | tooling | compose local; example local |
| `LOCAL_RELAY_NETWORK` | tooling | compose local; example local |
| `LOCAL_SANDBOX_IMAGE` | tooling | compose local; example local |
| `LOCAL_STACK_ID` | tooling | container local; py deploy-py/raw |
| `PGDATABASE` | tooling | container local |
| `PGHOST` | tooling | container local |
| `PGPASSWORD` | tooling | container local |
| `PGUSER` | tooling | container local |
| `TEST_DB_HOST` | tooling | py scripts/raw |
| `TEST_DB_PASSWORD` | tooling | py scripts/raw |
| `TEST_DB_PORT` | tooling | py scripts/raw |
| `TEST_DB_USER` | tooling | py scripts/raw |
