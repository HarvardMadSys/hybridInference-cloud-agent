# 环境变量盘点、门禁与统一前端管理（P1 第二次修订）

- 日期：2026-10-29
- 盘点基线：Cloud Agent `dev@4b7b826d`（#1034 合并后）加本分支的扫描器。§3 的数字、§6 的例外表和附录 A 的「出现位置」
  都按这个基线生成；本次修订没有重新扫描，也没有改变它们。
- 本次修订：2026-10-30，基于分支提交 `322a8fd0`，只改本文、实施计划、验证记录和分类表的说明文字。
- 参照：HybridInference `refs/codex/env-inventory-reference`（`3098cb9f`）中的
  `apps/backend/serving/config/app_config_registry.py`、
  `tests/unit/config/test_app_config_registry.py`、`tests/unit/config/test_no_raw_env_reads.py`
- 性质：两部分，标注分开。
  - **已实现**（本分支）：盘点和门禁。不迁移任何产品设置，不改默认值、安全策略或网络策略。
  - **提议**（未实现）：每个变量族的目标管理方式（§4.2、§7）、控制与恢复边界（§8）、统一的 Admin 体验（§9）。
    分阶段的实施计划见 [2026-10-30-unified-config-management-plan.zh-CN.md](2026-10-30-unified-config-management-plan.zh-CN.md)。
    提议部分不表示任何能力已经存在。
- 实现：`tests/support/env_scan.py`（扫描器）、`tests/support/env_shell.py`（shell 扫描器）、
  `tests/support/env_inventory.py`（分类表）、`tests/unit/test_env_classification.py`（门禁）、
  `tests/unit/test_env_scan.py`（扫描器自测）
- 验证记录：[盘点与门禁](../reviews/2026-10-29-env-inventory-gates.md)；
  [本次修订](../reviews/2026-10-30-env-inventory-ui-management.zh-CN.md)

## 0. 本次修订：用户纠正的产品目标

用户明确纠正了产品目标：尽可能多的配置应当能在前端管理。上一版把「今天由部署环境提供」写成了
「应当留在部署环境」「永不成为 admin 设置」，把当前来源和目标管理方式混成了一个判断。本次修订：

1. **分成两个轴**。**当前来源**：值今天从哪里来、谁在读（数据库设置行、心跳下发、部署环境、挂载文件、
   构建产物、运行时自述）。分类表的「去向」从此只表示这个轴，门禁据此检查当前代码。**目标管理方式**：
   管理员将来在前端用什么方式管理它（§4.2 的六种）。
2. 需要重启、属于密钥、在启动时就要用，这些都不再单独构成「不能在前端管理」的理由。它们决定的是用哪种方式
   （普通保存、受控应用、轮换或迁移流程）以及需要什么前提。
3. 撤回上一版「现在能搬进 Admin 的只有两项」「应当留在部署环境」「永不成为 admin 设置」这些结论。
   原来的 2 个 `candidate` 只是「可以直接复用现有设置机制」的一组，不是可前端管理的全集；3 个 `design`
   各缺一个具体机制，§10 给出了机制。
4. 引导和恢复用的输入仍然不放进它们所引导的存储（数据库设置行），否则会形成循环依赖，并在数据库或解密故障时
   失去恢复入口。但这不禁止前端经由独立的部署代理和它自己的存储来管理这些值（§8）。
5. 不放宽现有的沙箱、网络和凭据边界。前端可以在部署侧已审查的取值范围内选择、执行固定的经审查操作；
   不提供任意 shell，也不把 369 个原始变量逐个做成编辑框。
6. 已有的 UI 决定不变：按功能分组、字段直接可见、不用 Advanced 折叠、不恢复 #1034 移除的 Deployment 说明卡片。
   §9 提议的「部署」区是有真实操作和状态的功能页，与被移除的说明卡片不同（§9.3）。

不变的：盘点事实、扫描范围与边界、§3 的数字及其基线、门禁和例外表、已知欠账（§12）。门禁的分类约束的是当前来源
和读取点，不是对目标管理方式的永久限制（§6.5）。

## 1. 结论

### 1.1 今天的情况（已实现）

**已经在 Admin 里的**：`SETTINGS` 中带环境变量名的 64 项（其中 20 项在 `MIGRATED_SETTINGS`），
以及经心跳下发的 23 项 `HOST_SETTINGS` 和每台主机的 slots。这些变量今天在部署环境里只起
「首次导入」或「主机收到第一次心跳应答之前的引导值」的作用。

**不以变量形式出现、但已经能在前端管理的**：新会话使用的 Agent CLI 版本（`runtime_upgrades.py`，
Settings 页一键更新，主机下载校验后切换）、主机加入令牌（`POST /admin/agent/runner-hosts/enroll-token`）、
管理员注册的 compute profile 和自动 compute setup、sandbox 归档访问密钥（加密保存在 runner policy 里）。
托管 compute 主机的 `.env` 由控制面在设置时渲染（`compute_setup._assets`），并由控制面经 SSH
部署和升级到控制面自己的发布（`compute_reconcile._upgrade`、`SshHostBootstrap`）。

**其余**今天来自部署环境、挂载文件、构建参数或运行时自述。Admin 只对其中几项显示配置是否存在
（`deployment_setup.py` 的状态元数据；#1034 后不再作为设置页卡片显示）。

### 1.2 目标（提议）

按目标管理方式，369 个唯一名的分布如下（逐名见附录 A，每个族的理由见 §7）：

| 目标方式 | 唯一名 | 主要的族 |
|---|---|---|
| 编辑：普通保存 | 88 | 已在 Admin 的 F01、F03–F13（`AGENT_SANDBOX_MANAGER_TOKEN` 除外），可复用现有机制的 F14–F18 |
| 应用：受控部署、应用、重启 | 90 | pgAdmin 服务 F21、控制面监听与网络 F24、web 路由 F25、主机拓扑 F27、沙箱边界（限值内）F28、性能剖析 F29、部署入口 F31、Kata 版本 F32 |
| 流程：轮换或迁移 | 17 | 签名与加密密钥 F02、数据库连接与 `ADMIN_TOKEN` F19、设置根密钥 F33、sandbox manager 令牌、内置数据库的身份与口令、隧道 SSH 密钥 |
| 观测：只读事实 | 50 | 发布身份 F22、镜像构建输入 F30、旧名残留 F40–F41，以及由所选发布决定的沙箱镜像、只能观测的安装路径、Compose override 和两个放宽隔离的开关 |
| 对象：经所属对象管理 | 40 | 挂载文件与 compute profile F20、SSH 预填 F23、主机身份 F26、子进程注入 F34、AWS 凭据链 |
| 不开放 | 84 | 平台和第三方镜像变量 F38–F39、派生的管道变量 F35–F37、测试与本地栈 F42–F44、绕过部署安全检查的两个开关 |

这不是 285 个新编辑框。目标是按功能收敛成语义设置和操作：例如「控制面监听地址」是一个操作，背后是一组
`*_BIND`/`*_PORT`；「选择发布」是一个操作，背后是清单、提交和镜像引用。「不开放」的 84 个名字没有独立的运维含义
（`PATH`、测试接缝、Compose 把一组值映射给隧道容器的名字），由它们所属的语义项派生；将来如有需要也是给所属项
加选项，而不是开变量编辑框。

### 1.3 推荐的实施顺序

普通交付不等待最难的部署项目。详细的工作包、契约和验收见实施计划；顺序是：

1. 普通设置与导入欠账（WP1）、主机设置扩展（WP2）、管理目录与生效元数据（WP3），三者可以并行；
2. 控制面实例报告（WP4），让「已保存」和「已应用」分开显示；统一前端（WP5）随 WP3、WP4 分两步交付；
3. 部署代理与引导存储（WP6），先交付只读的「部署」区，再交付受控应用（WP7）和发布、Kata 操作（WP8）；
4. 管理员凭据（WP9）、密钥轮换（WP10）、同库连接更新（WP11）、换库迁移（WP12），各自有设计门。

## 2. 本 PR 做什么、不做什么

**做**（已实现）：

1. 一个 AST 扫描器，识别本仓库实际用到的环境变量读法（§6.1），以及 Compose、示例 env 文件、
   Dockerfile `ARG` 和前端里出现的变量名；另有一个有界的 shell 扫描器，找出 `deploy/` 和 `scripts/`
   下 11 个 shell 脚本消费的外部输入（§6.4）。
2. 一张分类表：每个变量（或一个变量族的前缀）恰好归到一个当前来源分类，并写明理由。
3. 在现有 `make test` 里新增门禁测试，CI 的单元测试分片自动执行，不需要改工作流：
   - 新出现的变量没有归类，测试失败；
   - 分类表里重复、互相覆盖、没有理由、或者已经没有任何地方用到的条目，测试失败；
   - `admin` 条目必须恰好等于 `SETTINGS` 的环境变量名，`host_admin` 条目必须恰好等于
     `HOST_SETTINGS` 的环境变量名加上主机 slots，`retired` 条目必须恰好等于仍在映射的旧名；
   - 后端代码绕过解析器直接读已注册设置，必须在精确的例外表里（模块、函数、变量名三元组）写明理由；
   - 主机设置只能在登记过的引导位置从主机环境读取，控制面不得读主机设置的环境变量；
   - 读不出变量名的读取只能是登记过的注册表循环；把整个环境交给子进程的地方也要逐一登记；
   - shell 脚本新增的字面量外部输入没有归类，测试失败；脚本里计算出来的间接读取（`${!name}`、
     `eval`、拼出来的名字）必须按（脚本、函数、写法）登记理由；
   - 新的带环境变量名的注册必须进 `MIGRATED_SETTINGS`（§5.1）；
   - 内部变量不得出现在运维模板里，工具类变量不得被后端读取或出现在生产模板里；
   - 每一条例外都必须仍然对应一处真实读取，不再需要时测试会要求删除。
4. 扫描器自测和每条规则的反例测试：每种读法都有样例，每条门禁都喂一个违规样本并断言失败。

**不做**：

- 不迁移任何设置，不改 `MIGRATED_SETTINGS`、`HOST_SETTINGS`、默认值或校验规则。
- 不实现任何目标管理方式：没有部署代理、部署存储、实例报告、轮换或迁库功能，也不改前端。
- 不锁定 `control_signing_secret`，不改任何密钥轮换流程。
- 不改 ENVIRONMENT.md 和其他维护中的指南；文档缺口记录在 §12。
- 不涉及 `MODEL_CONNECTIONS_ENABLED` 的混合版本安全、PostgreSQL `application_name`、SQL 触发器、
  部署上限变量、密钥轮换和生产配置。
- 分类表只放在 `tests/support/` 下，不进入任何运行时镜像（各 Dockerfile 只复制 `apps/backend` 下的三个包）。

## 3. 盘点口径与数字

数字由门禁本身的扫描得出（`git ls-files` 加上未被忽略的新文件），基线是 `4b7b826d` 加本分支的扫描器，可以重现；
按名字计数和按读取点计数分开报告，重叠的表不相加。

| 口径 | 数量 |
|---|---|
| 唯一变量名（含 1 个动态前缀 `AGENT_SECRET_PATTERN_*`） | 369 |
| 其中 Python 读取的唯一名 | 166 |
| 其中模板、构建文件或前端出现的唯一名 | 252 |
| 其中 shell 脚本消费的外部输入（唯一名） | 116，其中 36 个只出现在 shell 里 |
| Python 与模板都出现的 | 102 |
| 只存在于注册表里的（没有任何读取点或模板） | 4（四个网页搜索变量，见 §12） |
| Python 读取记录（所有扫描范围） | 315：`raw` 184、`resolver` 88、`copy` 28、`compat` 10、`helper` 5 |
| 其中后端（`apps/backend`）读取记录 | 253，涉及 132 个唯一名 |
| 模板引用记录 | 758：Compose 替换 272、容器 `environment:` 键 258、示例 env 157、Dockerfile `ARG` 39、前端 25、Compose 构建参数 7 |
| shell 输入记录（11 个脚本，每个脚本每个名字记首次） | 153：经辅助函数 55、未赋值即读 41、字面量循环 28、自身默认值 25、赋值前带默认的读取 4；计算出来的读取 0 |

按当前来源分类（分类表的 `Disposition`，唯一名）：

| 分类 | 数量 | 含义（只描述当前） |
|---|---|---|
| `admin` | 64 | 已注册为 Admin 设置：环境首次导入，之后数据库为准 |
| `host_admin` | 24 | 主机设置，经心跳下发；主机环境只是第一次应答前的引导值 |
| `candidate` | 2 | 今天从环境读；可以直接复用现有设置机制 |
| `design` | 3 | 今天从环境读；复用现有机制之前要先补一个具体机制 |
| `deployment` | 154 | 今天由部署环境、挂载文件、构建参数或部署脚本输入提供 |
| `operation` | 1 | 今天由部署环境提供，改值需要专门流程（设置根密钥） |
| `internal` | 39 | 我们的代码注入子进程，运维不设 |
| `platform` | 25 | 操作系统、Docker、sshd、AWS SDK、第三方镜像的变量 |
| `retired` | 10 | 兼容期内的旧名，或已不再读取 |
| `tooling` | 47 | 测试、CI、基准、本地开发栈、脚本的测试接缝 |

分类表本身有 44 个族、327 个精确名和 6 个前缀（`AGENT_TUNNEL_`、`AGENT_MCP_TUNNEL_`、
`AGENT_CONTROL_PLANE_TUNNEL_`、`AGENT_SECRET_PATTERN_`、`PGADMIN_`、`LOCAL_`）。前缀只用在
ENVIRONMENT.md 也按族记录的隧道变量、按前缀扫描的内部变量、第三方 pgAdmin 镜像的命名空间和本地开发栈上；
本地开发栈前缀另有约束：这些名字不得被后端读取，也不得出现在 `deploy/control-plane`、`deploy/host` 的模板里。
本文按分类表里的顺序把 44 个族编号为 F01–F44（§7）。

逐名的当前分类、族、目标方式和出现位置见附录 A。

## 4. 两个轴

### 4.1 当前来源分类（已实现，门禁检查的轴）

分类只回答「值今天从哪里来、谁读」。是否是密钥、是否需要重启、是否被多个进程读取、改了能否撤回，
是另一组属性，写在各条理由里，不拆成互斥的类别。最初草稿的九类在这里合并如下：

- 草稿的 X（边界参数）、D（部署层）都归 `deployment`。会放宽隔离或扩大可达范围的变量单独成一个族（F28），
  理由写明它属于 AGENTS.md §7 的边界。
- 草稿的 O（操作）只保留今天仍由环境提供的根密钥 `AGENT_SETTINGS_ENCRYPTION_KEY`。已经注册为设置的
  几把密钥仍是 `admin`，它们「替换即失效」的性质写在理由里。
- 草稿的 M（双镜像）和 A（集群断言）对应 `design`：今天没有机制保证两边一致或集群事实成立。
- 草稿的 G（守卫设置）和 S¹（单向设置）是对已注册设置的改进建议，不是分类。

同一个名字可能有多个读者和含义（§5.2）。分类按名字给出一个值，是为了让门禁可检查；附录 A 用 † 标出多含义的名字，
它们的每个含义在 §5.2 分别给出当前来源和目标。

### 4.2 目标管理方式（提议）

| 方式 | 前端上是什么 | 值的权威副本 | 「生效」的证据 |
|---|---|---|---|
| **编辑** | 设置页的字段，Review 后按 CAS 保存 | 数据库设置行（或主机设置） | 已保存 = revision；已采纳 = 各控制面实例或主机回报的 revision（WP4；主机今天已有 `host-settings-v1` 和 `settings_local`） |
| **应用** | 表单加一个「应用」操作，显示进度和结果；需要重启的服务在操作里明说 | 部署代理的存储（§8）；控制面只存意图、密文和审计 | 代理报告的已应用配置版本，加上服务健康检查和心跳 |
| **流程** | 多步操作，有持久化的操作记录（阶段、游标），可重入，有明确的回滚边界 | 视流程而定（数据库或部署存储），从不同时存两份明文 | 每个参与者对新阶段的确认；未确认前旧材料不退役 |
| **观测** | 只读的事实和状态 | 运行中的进程、主机或代理的自述，发布清单 | 证据本身；没有证据显示「未知」 |
| **对象** | 在所属对象的页面管理（模型连接、MCP 连接、主机、compute profile、Agent CLI 版本、归档策略） | 该对象的存储 | 该对象自己的状态 |
| **不开放** | 不出现 | 平台、镜像或派生 | — |

**限值**：属于 AGENTS.md §7 边界的值（F28 和 Kata 运行时）用「应用」时，只能在部署代理本地策略列出的已审查取值内选择，
或者只能朝收紧的方向改（例如受保护网段只能增加）。本地策略文件不经前端修改；放宽边界仍是设计变更。

**期望与实际**：操作员选择的是期望（期望发布、期望配置版本）；实际的提交、能力、已应用版本只来自证据，
不是可编辑的断言。

### 4.3 两轴的关系

当前分类不决定目标方式。`deployment` 的 154 个名字分布在应用、流程、观测、对象和不开放五种目标方式里；
`admin` 的 64 个大多是编辑，其中 4 把密钥和 sandbox manager 令牌是流程。一个变量在实现目标方式之后，
如果它的当前来源也变了（例如注册进 `SETTINGS`），分类表随之改动并由门禁检查；如果当前来源没变
（例如部署代理改的是 `.env`，进程仍从环境读），分类不变，只是 `.env` 的写入者从人换成了代理。

## 5. 迁移约束（当前机制，已核实）

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

### 5.2 同名多读者：每个含义分别建模

注册之后，服务进程通过 `setting_env` / `get_setting` 只看数据库快照。但同名环境变量仍可能有别的读者，
门禁把后端的每一处都登记在 `RAW_SETTING_READS` 里。这些名字不能被赋予一个「唯一来源」，前端也要按含义分别显示。
下表「当前」一列是已核实的事实，「目标」一列是提议。

| 名字 | 当前的读者和含义 | 目标（按含义） |
|---|---|---|
| `AGENT_SESSION_SECRET` | ① 会话签名：数据库里的设置值。② 设置根密钥的回退：`domain_cipher.settings_root_key()` 在 `AGENT_SETTINGS_ENCRYPTION_KEY` 未设置时读环境里的这个值。只有在回退状态下，改环境值才会改变根密钥，进而让已保存的加密设置无法解密；设置了专用根密钥时，改环境值对根密钥和会话签名都没有作用 | ① 显式替换（今天已可，后果是全部登出），放在流程里是为了统一显示后果。② 流程：用回退的相同字节建立专用根密钥（WP10），之后环境值不再有根密钥含义 |
| `AGENT_PGADMIN_URL` | ① 保存的 upstream（数据库）。② `deployment_pgadmin_services()` 直接读环境，与内置的 `http://pgadmin:5050` 和 `AGENT_PGADMIN_BOOTSTRAP_URL` 一起构成「部署允许的 pgAdmin 服务」；保存的 URL 不在这个集合里时，`pgadmin_config()` 返回 503，所以去掉环境里的自定义条目只会让自定义地址不可用，不会变成开放代理 | ① 编辑（现有）。② 应用：允许服务列表成为部署代理管理的部署配置（WP7）；允许非内置服务之前先做 SSRF 边界设计 |
| `AGENT_SANDBOX_MANAGER_TOKEN` | 控制面那份在数据库里；runner 和 sandbox manager 各读自己的环境；托管主机的值是设置主机时从数据库写进它 `.env` 的快照（`compute_setup._assets`），之后不会自动跟随 | 流程：双令牌窗口，主机先同时接受新旧两把，控制面切换，主机撤销旧的；托管主机由控制面重新渲染 `.env`，自管主机由主机代理（WP10） |
| `AGENT_LOG_LEVEL` / `AGENT_LOG_FORMAT` | 控制面在第一次采纳快照之前用环境值配置日志，之后由 `apply_settings` 换成保存值；主机进程把同名变量当作自己的部署配置 | 控制面：编辑（现有）。主机：另作主机级设置（WP2，需要字符串型主机设置）；主机进程能否不重启换日志配置未核实，先标需重启 |
| `AGENT_CLOUDLAB_API_URL` | `ComputeConfiguration.from_env()` 读进一个回退字段；控制面始终以 `with_runtime_settings()` 运行，请求时读保存值 | 编辑（现有）；回退字段不显示 |
| `AGENT_COMPUTE_SECRET_KEY` | 数据库设置（设置后锁定）；控制面 `deploy.sh` 首次部署时生成并写进 `.env`，供首次导入 | 流程：重加密（WP10）。首次生成保留在冷启动路径里（部署代理初始化时做同样的事） |
| `AGENT_GITHUB_APP_PRIVATE_KEY` / `_PATH` / `_FILE` | 内联私钥（数据库）不为空时优先；为空时用挂载文件 | 内联：编辑（现有），页面显示当前用的是内联值还是挂载文件；挂载文件：对象（GitHub App 设置页），替换文件是可选的应用操作 |
| `AGENT_EGRESS_PROTECTED_CIDRS` | 主机执行（沙箱网络）；控制面那份只用于 SSH 服务器检查和模型连接校验的拨号 | 应用（限值内）：两边来自同一份部署配置，前端只能增加受保护网段，删除要经部署侧策略；控制面那份是否改为从主机回报推导是 WP7 要回答的问题 |
| `AGENT_RUNTIME_REGISTRY_URL` | 控制面用它列版本，每台 runner 用它下载；两边必须相同 | 编辑：一个设置项，经字符串型主机设置下发，主机先采纳并回报，控制面再切换（WP2） |
| `AGENT_DEPLOYMENT_SHA` | 控制面、主机和 web 各自陈述自己的发布；就绪围栏要求主机以精确提交回报 | 观测。操作员选择期望发布（WP8），实际提交只来自证据 |
| `GATEWAY_GRANT_DISPATCH_TOKEN` | 数据库设置；本地栈的辅助脚本直接读环境 | 编辑（现有，Gateway URL 改了要重新输入）；本地栈不受影响 |
| `ADMIN_TOKEN` | `verify_admin_access` 在每个请求里读环境。它授予**完整的管理员权限**：所有管理路由、owner 参数例外、on-call 管理兜底和审计主体，并且是 Gateway 登录坏掉时 `/setup` 的入口 | 流程：经部署代理轮换（写部署存储并重启 API 和 MCP listener，WP9）；另建数据库管理的日常管理凭据。它不是一把窄范围的恢复令牌，在 WP9 改变权限语义之前不能这样描述 |
| `AGENT_DATABASE_URL` | 控制面 lifespan 建连接池、迁移、`deploy_readiness` 一次性命令 | 流程：同库的连接更新（WP11）和把数据搬到另一个库（WP12）是两件事 |
| 主机设置（F11–F13） | 管理员值经心跳下发；主机环境是第一次应答前的引导值，并作为 `settings_local` 回报 | 编辑（现有）；本地值作为观测显示 |

不经过 `setting_env` 的一次性工具（`deploy/`、`scripts/` 下的脚本）仍然直接读环境；门禁只对
`apps/backend` 执行解析器规则。

### 5.3 主机设置复用心跳语义

`HOST_SETTINGS` 的优先级是「单台主机的值 > 全局值 > 内置默认值」。控制面在每次心跳应答里下发完整的
生效配置；主机的环境变量只在收到第一次应答之前起作用，并作为 `settings_local` 回报用于诊断；
清除管理员值不会让旧环境值重新生效。控制面总是在应答里下发这些值；主机用 `host-settings-v1` 能力声明自己会应用它们，管理页据此区分「已应用」和「仍在用本地值」。新的主机设置应当直接加进
`HOST_SETTINGS`，不需要另建分发服务。

门禁把主机从自己环境读取主机设置的位置逐一登记在 `HOST_BOOTSTRAP_READS`。其中一处是已知欠账：
`runner._shared_live_host_capabilities` 校验的是本地的 `AGENT_SHARED_RESIDENT_CAPACITY`，
不看管理员值；本地值越界时，即使管理员值合法，这台主机也不会声明 shared-live 能力。本 PR 不改它。

`AGENT_HOST_SLOTS` 不在 `HOST_SETTINGS` 里，但走的是同一种「管理员值经心跳下发、环境值作默认」的路径
（`PUT /admin/agent/runner-hosts/<host>/slots`），所以也归 `host_admin`，测试里单独列出。

## 6. 门禁（已实现）

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
- shell 脚本里看不到的读法见 §6.4 末尾；`.claude/hooks/` 下的脚本属于编码代理的工具配置，不在扫描范围内；
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
| `SHELL_HELPERS` | 函数名 | 1 条：`artifact_env_value`（在渲染好的 Compose 环境里按名字查值，模式认不出） |
| `SHELL_NOT_INPUTS` | （脚本，变量） | 1 条：`artifact_env.sh` 里的 `SHA`，由 source 它的两个 `deploy.sh` 先设好 |
| `SHELL_DYNAMIC_READS` | （脚本，函数，写法） | 0 条：现有的间接读取都能解析成字面量循环，或位于辅助函数内部 |

所有例外都是精确键，没有按目录或按模块整体豁免（两个解析器模块除外）；每条都有理由，并由
`test_every_exception_is_still_needed` 检查仍然需要。没有使用 `xfail`。

### 6.4 shell 脚本

范围：`deploy/` 和 `scripts/` 下所有 `.sh`，共 11 个：两个 `deploy.sh`、`sandbox_host.sh`、
`setup_kata_runtime.sh`、`agent-gateway-tunnel.sh`、`deploy/local/dev.sh`、`deploy/release/artifact_env.sh`、
沙箱镜像构建用的 `build-kilo.sh` 和 `install-runtimes.sh`、`scripts/ci-local.sh`、`scripts/test-db-local.sh`。
实现是 `tests/support/env_shell.py`：一个词法预处理加几条模式，不是 shell 解释器。

先把 shell 不展开的内容抹掉（注释、单引号和 `$'...'` 字符串、带引号定界符的 here-document 正文），
保留行号；然后对每个脚本区分：

- **引用**：`$NAME`、`${NAME...}`，以及 `$(( ))`/`(( ))` 里的裸名。位置参数和特殊参数（`$1`、`$@`、
  `$#`、`$?`……）和 bash 自己的变量（`BASH_SOURCE`、`IFS`、`RANDOM`、`PWD`……）永远不算输入。
- **赋值**：命令开头的 `NAME=`（包括 `;`、`&&`、`then`、`case` 分支之后），`local`/`declare`/`typeset`
  声明的名字，带值的 `export`/`readonly`，`for NAME in`，以及 `read`、`mapfile`/`readarray`、`getopts`、
  `printf -v` 填写的名字。不带值的 `export NAME` 是把继承来的值传下去，不算赋值；`NAME=value command`
  这种前缀赋值是给子进程设值，算赋值，不算读取。
- **输入**：被引用，并且满足其一：脚本里从未赋值；第一次赋值就是给自己设默认值（`NAME="${NAME:-x}"`、
  `${NAME:=x}`）；或者在第一次赋值之前带默认值读取（`${NAME:-}`、`${NAME-}`、`${NAME:+}`），也就是
  `set -u` 下表示「可能来自外部」的写法。
- **辅助函数**：读取第一个参数所指变量的函数，即 `${!1}`、`local name="$1"` 加 `${!name}`，或者把
  `"$1"`/`"$name"` 交给另一个辅助函数；`for name in "$@"` 的函数对每个参数都这样处理（`require_env A B`）。
  `env_or_dotenv AGENT_HOST_SLOTS` 这样的字面量调用记为对 `AGENT_HOST_SLOTS` 的读取。用别的办法查值的函数
  （`artifact_env_value`）在 `SHELL_HELPERS` 里声明。
- **循环**：`for name in A B C` 里的 `${!name}` 和 `helper "$name"`，以及 `helper "${prefix}_SSH_KEY"`
  （`prefix` 来自字面量循环），按最近的那个循环展开成具体名字。读回脚本自己赋过值的名字不算输入。
- 其余的 `${!name}`、用计算出的参数调用辅助函数、`eval`，报告为**计算读取**，必须在
  `SHELL_DYNAMIC_READS` 里按（脚本、函数、写法）登记。`${!array[@]}` 列的是数组下标，不是读取。

结果：11 个脚本共消费 116 个外部输入，没有计算读取。其中 80 个也出现在 Python 或模板里，归类不变；
36 个只出现在 shell 里，新增为下列几个族：

| 去向 | 名字 | 说明 |
|---|---|---|
| `deployment` | `DEPLOY_SHA`、`SOURCE_REPO`、`DEPLOY_ROOT`、`APP_DIR`、`ALLOW_DIRTY`、`ALLOW_STRAY_STACK`、`HEALTH_TIMEOUT`、`AGENT_DEPLOY_MODE`、`AGENT_DEPLOY_PYTHON`、`AGENT_DEPLOY_DRAINING_HOST`、`AGENT_RELEASE_MANIFEST`、`AGENT_WEB_VARIANT` | 部署入口的输入：部署哪个提交、从哪里取、装到哪里、等多久、安装哪个制品版本。每次部署都可能不同，由运行部署的人或流水线提供，不进 Admin |
| `deployment` | `KATA_VERSION`、`KATA_SHA256`、`KATA_DOWNLOAD_BASE`、`KATA_LINK_DIR`、`KATA_CONFIG_PATH`、`KATA_DEBUG_KERNEL_PATH`、`KATA_SETUP_SCRIPT` | 主机安装的 Kata 运行时：主机的隔离边界，按版本固定、按摘要校验 |
| `internal` | `DEFER_SHARED_READY_MARKER`、`WATCH_READINESS`、`TUNNEL_SSH_KEY`、`TUNNEL_SSH_KNOWN_HOSTS` | 一个脚本给它自己启动的脚本或容器设置的值 |
| `retired` | `AGENT_RUNNER_REPLICAS` | 已不再读取，`sandbox_host.sh` 发现它时只提醒删除 |
| `tooling` | `KATA_EXTRACT_ROOT`、`KATA_CPUINFO`、`KATA_KVM_DEVICE`、`DOCKER_DAEMON_JSON` | 主机脚本留给测试的接缝 |
| `tooling` | `CI_LOG_DIR`、`DEPLOY_PEER_JOB`、`TEST_DB_LOCAL_*`（4 个）、`TEST_DB_WORKERS`、`UV_RUN` | 本地 CI、部署工作流的对端作业、本机测试数据库 |

shell 脚本还读 5 个已注册设置的名字，都不是绕过解析器：控制面 `deploy.sh` 在首次部署时生成
`AGENT_COMPUTE_SECRET_KEY` 并写进 `.env`，供首次导入；`deploy/local/dev.sh` 给本地栈的
`GATEWAY_AUTHORIZE_URL`、`AGENT_SESSION_REDIRECT_URI`、`AGENT_WEB_BASE_URL` 设默认值；`sandbox_host.sh`
读主机自己那份 `AGENT_SANDBOX_MANAGER_TOKEN`（§5.2）。主机设置名（`AGENT_HOST_SLOTS`、
`AGENT_SHARED_RESIDENT_CAPACITY` 等）在 `sandbox_host.sh` 里只用于部署前检查本地引导值。

shell 部分看不到的：只由调用方赋值、函数里读取的变量（`SHA` 这种 source 关系用 `SHELL_NOT_INPUTS`
逐条说明）；source 进来的文件设的值；交给另一个 shell 的字符串里的读取（`bash -c`、`ssh`、Compose 的
`command:`）；用 `grep`/`sed` 直接从 `.env` 里按名字取值（控制面 `deploy.sh` 对 `AGENT_COMPUTE_SECRET_KEY`
就是这样，这个名字另外也经 `${...}` 读取，所以仍被分类）；函数内部的执行顺序（在函数里赋值前的带默认读取，
按行号判断）。这些写法今天没有让任何输入漏掉，但门禁也证明不了以后不会。

### 6.5 门禁与目标管理方式

门禁检查的是当前来源和读取点：哪些名字被谁读、注册表和分类是否一致、绕过解析器的读取是否登记。
它不规定一个变量将来能不能在前端管理。具体地：

- 分类为 `deployment`、`operation`、`internal` 的名字，实现前端管理时不需要先改分类，除非当前来源变了。
  部署代理改写 `.env`、进程仍从环境读的值，分类保持 `deployment`。
- 把一个名字注册进 `SETTINGS` 或 `HOST_SETTINGS`，门禁要求同时把它移到 `admin` / `host_admin`，并进
  `MIGRATED_SETTINGS`（§5.1）。这是有意的：注册改变了已有部署的导入行为，应当在评审里看得见。
- 分类表的理由描述当前事实；目标方式写在本文 §7 和附录 A，不写进分类表，也不由门禁检查。
  这次只改了分类表里与新目标矛盾的说明文字（模块说明、分类注释和两条族理由），没有改门禁逻辑。
- 实施计划的 WP3 会在产品代码里建立语义管理目录；到那时是否增加一条「每个非『不开放』的族都对应至少一个目录项」
  的测试，在 WP3 评审时决定。

## 7. 两轴映射（按族）

每行一个族，编号是分类表里的顺序（F01–F44），括号里是唯一名数。「当前」一列是已核实的事实（本分支基线），
其余三列是提议。工作包编号（WP1–WP12）见实施计划。只有族一级证据的地方不细到单个变量；附录 A 中与所属族目标方式
不同的名字用＊标出，原因写在对应行里。

### 7.1 已在 Admin（F01–F13）

| 族 | 当前来源与读者 | 目标方式与后端归属 | 前提 | 生效、重启与回滚 |
|---|---|---|---|---|
| F01 Gateway 登录与授权分发（11） | 设置行，控制面经解析器读；`deploy/local/dev.sh` 给本地栈设默认值 | 编辑。`settings.py`、`api/admin_settings.py`、Settings 页（现有） | WP3 按读取点标注生效方式 | 各控制面进程约 5 秒内采纳快照，下一个请求生效；`GATEWAY_ISSUER` 在身份命名空间绑定后锁定，换发行方是身份迁移，不在本计划；回滚 = 保存上一个值 |
| F02 签名与加密密钥（4） | 设置行；`AGENT_SESSION_SECRET` 另有根密钥回退含义（§5.2）；`AGENT_COMPUTE_SECRET_KEY` 由 `deploy.sh` 首次生成 | 流程。会话签名替换保留为显式动作；控制签名密钥改为双密钥轮换；两把加密密钥改为重加密流程。提议 `application/key_rotation.py` 加操作日志表（WP10） | WP4 进程确认；WP10 的数据族清单 | 每步有持久化阶段；旧密钥在所有参与者确认前不退役；失败回到上一阶段 |
| F03 源码管理与发布（14） | 设置行；内联 App 私钥为空时用挂载文件（F20） | 编辑（现有） | 页面显示当前用内联值还是挂载文件 | 下次调用代码托管平台时生效（WP3 核对）；回滚 = 保存旧值 |
| F04 MCP 登录与拨号策略（5） | 设置行；两个 ALLOW 开关在 MCP listener 重启后对新连接生效 | 编辑（现有）；重启 MCP listener 是一个应用操作（WP7） | WP3 标注需重启；WP4 报告 listener 是否已采纳 | 未重启前显示「待重启」，不显示已生效 |
| F05 控制面运维（6） | 设置行；日志两项另有启动前和主机含义（§5.2） | 编辑（控制面，现有）；主机日志另作主机级设置（WP2） | 字符串型主机设置 | 控制面采纳快照时重配日志 |
| F06 外观（8） | 设置行；web 服务向控制面取保存值，`AGENT_CONTROL_PLANE_URL` 未设时才回退读自己的环境 | 编辑（现有） | 无 | 下次页面渲染（WP3 核对缓存） |
| F07 网页搜索（4） | 设置行；受支持的 Compose 不传、ENVIRONMENT.md 无行、不在 `MIGRATED_SETTINGS` | 编辑（现有）；修导入欠账（WP1） | 进 `MIGRATED_SETTINGS`，补 ENVIRONMENT.md 及中文翻译 | 已有数据库升级后对缺失的键导入一次 |
| F08 Compute 端点（2） | 设置行；CloudLab URL 另有回退字段（§5.2） | 编辑（现有） | 无 | 请求时读 |
| F09 归档与数据库工具（6） | 设置行；`AGENT_PGADMIN_URL` 另被读作部署允许列表（§5.2） | 编辑（保存值，现有）；允许列表见 F21 | 见 F21 | 保存的 upstream 不在允许列表时返回 503 |
| F10 执行策略（4） | 设置行；`AGENT_SANDBOX_MANAGER_TOKEN` 每台主机另有一份（§5.2） | 编辑；manager 令牌＊为流程（WP10） | 自管主机的令牌更新需要主机代理（WP6） | 默认执行策略对新会话生效 |
| F11 runner 主机设置（8） | 全局值和每台主机值经心跳应答下发；主机环境只在第一次应答前起作用，作为 `settings_local` 回报 | 编辑（现有：Settings 与 Servers 页；`host_settings.py`、`api/admin_hosts.py`） | 修 shared-resident 能力检查用本地值的欠账（WP2） | 下一次心跳；`host-settings-v1` 能力区分「已应用」和「仍用本地值」 |
| F12 sandbox manager 主机设置（15） | 同上，由 runner 交给 sandbox manager | 编辑（现有） | 无 | 同上 |
| F13 主机槽位（1） | Servers 页 `PUT .../slots`，经心跳；环境为默认值 | 编辑（现有） | 无 | 下一次心跳 |

### 7.2 可复用现有机制（F14–F18，原 `candidate` / `design`）

| 族 | 当前来源与读者 | 目标方式与后端归属 | 前提 | 生效、重启与回滚 |
|---|---|---|---|---|
| F14 每账号事件流上限（1） | 控制面在每次占用时读环境 | 编辑：进 `MIGRATED_SETTINGS`（WP1） | 取值范围包住部署里可能已有的值；描述写明是每个进程的上限 | 采纳快照后的下一次占用；升级时导入一次环境值 |
| F15 runner 租期（1） | runner 启动时读进 argparse 默认值 | 编辑：进 `HOST_SETTINGS`（WP2） | runner 每次认领使用生效值并传给回合子进程；下限大于一次心跳间隔加一次请求超时（30 + 10 秒），上限不超过控制面的 900 秒 | 下一次认领；旧 runner 丢弃新键继续用环境值，页面显示仍用本地值 |
| F16 运行时注册表（1） | 控制面列版本、每台 runner 下载，都读环境；两边必须相同 | 编辑：一个设置项，经字符串型主机设置下发（WP2） | `HostSetting` 支持字符串；主机回报正在用的注册表；切换时主机先采纳，控制面再用 | 回滚按同样顺序改回 |
| F17 模型连接开关（1） | 经 `setting_env` 读但未注册，每个副本看自己的环境 | 编辑：开关只在所有在线控制面实例都声明支持模型连接时可打开（WP4 之后） | WP4 的实例报告提供「集群里没有旧副本」的证据；混合版本安全、`application_name`、SQL 触发器是这一项的设计输入 | 关闭的安全性同样由该设计回答，本文不预设 |
| F18 API 文档路由（1） | `create_app()` 构建路由时读，此时没有设置快照 | 编辑：文档路由改为按请求判断（WP1） | 小的路由改动；默认关闭不变 | 采纳快照后的下一个请求 |

### 7.3 今天由部署提供（F19–F32）

| 族 | 当前来源与读者 | 目标方式与后端归属 | 前提 | 生效、重启与回滚 |
|---|---|---|---|---|
| F19 控制面引导（2） | 部署环境；`AGENT_DATABASE_URL` 与 `ADMIN_TOKEN` 的读者见 §5.2 | 流程。数据库：同库连接更新（WP11）与换库迁移（WP12）分开；`ADMIN_TOKEN`：经部署代理轮换，并另建日常管理凭据（WP9）。两者的值只在部署存储里 | 部署代理与存储（WP6）；实例报告（WP4） | 写部署存储 → 逐实例重启 → 健康检查 → 记录；失败回到上一份部署配置；换库在恢复写入后不切回旧库 |
| F20 挂载文件与 compute 配置（4） | 控制面读挂载路径；`AGENT_COMPUTE_CONFIG` 是可信部署 profile（引用部署密钥），今天的 Admin API 不接受 | 对象：GitHub App 私钥用内联设置（现有）；compute profile 在 Compute setup 注册（现有），文件 profile 在同一处按来源只读显示；替换挂载文件是可选的应用操作（WP7） | 同名 profile 的覆盖规则要在页面上显示 | 文件替换需要重启控制面 |
| F21 pgAdmin 首次设置（6） | 部署环境和私有卷；pgAdmin 容器和控制面允许列表读取 | 应用：启用或停用 pgAdmin 服务（连同 `COMPOSE_PROFILES`）、首次设置地址、Cookie secure，由部署代理渲染并重启（WP7） | 允许列表扩大到非内置服务之前先做 SSRF 边界设计 | 重启 pgAdmin 和控制面；回滚到上一配置版本 |
| F22 发布身份（9） | 构建参数和部署时写入；控制面、主机、web 各自陈述；就绪围栏按精确提交匹配 | 观测。操作员选择期望发布（F31，WP8）；实际提交来自心跳、实例报告和代理报告 | WP4、WP6 | 不可编辑 |
| F23 SSH 地址预填（2） | 只用来预填自动 compute setup 表单，保存的请求优先 | 对象：Compute setup 表单（现有） | 可选：表单记住上次保存的请求 | 无运行时作用 |
| F24 控制面监听、网络、数据库容器与 Compose（15） | 控制面机器的 `.env`，`deploy.sh` 和 Compose 读取 | 应用：监听地址与端口、edge、网络名、性能剖析部署标记由部署代理改 `.env` 并重建相关服务（WP7）。`AGENT_POSTGRES_*`＊是内置数据库的身份和口令，改动走数据库流程（WP11）。Compose override 文件＊只观测：内容是任意容器配置 | 改控制面自己的监听地址会让当前浏览器断开：代理先检查新地址可达，保留上一版本 | 健康检查不过时代理恢复上一配置版本；前端断连期间以代理本地记录为准（§8.4） |
| F25 web 构建路由（5） | web 镜像构建时代入（`root` / `agents` 两个变体）；web 服务读控制面地址 | 应用：web 变体随发布选择（WP8）；控制面地址是拓扑（WP7） | 基础路径的改动必须与 F01 的回调地址在同一个操作里提交 | 重建 web；回滚到上一变体 |
| F26 主机身份与加入（6） | 主机环境和凭据文件；加入令牌今天已可在 Admin 签发 | 对象：主机（Servers 页签发加入令牌、显示身份）；重新加入、凭据撤销是流程（WP10） | 无（签发已有） | 主机身份只观测 |
| F27 主机拓扑、挂载与路径（49） | 自管主机：主机 `.env`；托管 compute 主机：控制面在设置时渲染 `.env` 并经 SSH 部署和升级 | 应用：每台主机的部署配置，由主机代理（自管）或现有 SSH 执行器（托管）渲染，并重启 runner、sandbox manager、隧道（WP7）。隧道 SSH 密钥＊（6）走轮换流程；数据根和状态目录的改动等同于搬数据，也按流程处理 | 主机代理（WP6）；SSH 执行器与代理共用同一份配置模式 | 先排空再重启；失败恢复上一配置；结果经心跳和代理报告 |
| F28 沙箱隔离与网络（14） | 主机环境；控制面另读 `AGENT_EGRESS_PROTECTED_CIDRS`（§5.2） | 应用（限值内）：只能在主机代理本地策略列出的已审查取值内选择，受保护网段只能增加。`AGENT_SANDBOX_ALLOW_UNISOLATED`、`AGENT_SANDBOX_EXTRA_ARGS`＊只观测（任何非默认值都是放宽）；`AGENT_SANDBOX_IMAGE`＊由所选发布决定，只观测 | 本地策略不经前端修改；放宽边界仍是设计变更（AGENTS.md §7） | 排空后重启；回滚到上一配置 |
| F29 性能剖析（4） | 控制面和主机环境；`MODE` 是部署上限，`disabled` 时连 Admin 控件也拒绝 | 应用：由部署代理改并重启；Admin 里现有的剖析控件继续在上限内工作 | 提高上限是放宽，需要单独确认 | 重启相关进程 |
| F30 镜像构建输入（19） | Dockerfile `ARG`，发布流水线构建时代入 | 观测：发布内容，读自发布清单的运行时目录。新会话用的 Agent CLI 版本已在 Admin 里管理（runtime upgrades，现有） | 无 | 换构建输入意味着产生新的发布 |
| F31 部署入口输入（12） | 运行部署的人或流水线交给 `deploy.sh`、`sandbox_host.sh`、`artifact_env.sh` | 应用：成为部署代理操作的参数——选择发布清单、阶段、等待时间、要排空的主机（WP8）。`DEPLOY_ROOT`、`APP_DIR`、`AGENT_DEPLOY_PYTHON`＊是代理自身的安装位置，只观测；`ALLOW_DIRTY`、`ALLOW_STRAY_STACK`＊绕过部署安全检查，不开放 | WP6 | 阶段可重试；`abandon` 清理冷启动时启动的监听 |
| F32 Kata 运行时（7） | `setup_kata_runtime.sh` 的覆盖值，按版本固定、按摘要校验 | 应用：在已审查的「版本—摘要」对中选择，由主机代理排空后安装并校验（WP8）。下载源、链接目录、配置路径、调试内核和安装脚本路径＊属于代理本地配置，只观测 | 已审查的版本—摘要对来自发布的主机配方 | 安装或校验失败时保留旧运行时；回滚到上一对 |

### 7.4 设置根密钥（F33）

| 族 | 当前来源与读者 | 目标方式与后端归属 | 前提 | 生效、重启与回滚 |
|---|---|---|---|---|
| F33 设置根密钥（1） | 控制面读环境，用来解密设置行；未设置时用环境里的 `AGENT_SESSION_SECRET` 回退 | 流程（WP10）：先用回退的相同字节建立专用根密钥（密文不变，但要所有进程确认），再做真正的轮换（重加密此密钥保护的密文：设置行的 secret 字段和 runner policy 的归档凭据）。新密钥只写进部署存储，不进数据库 | WP6、WP4 | 每步可重入；所有进程确认前旧密钥保留 |

### 7.5 内部、平台、旧名和工具（F34–F44）

| 族 | 当前来源与读者 | 目标方式 |
|---|---|---|
| F34 交给子进程的值（24） | 主机给它为成员或操作启动的进程设置 | 对象：模型连接、MCP 连接、插件、环境脚本和密钥扫描规则各自的页面；不出现为变量 |
| F35 reaper 源路径（1） | 镜像内部覆盖 | 不开放 |
| F36 隧道容器映射与锁（12） | Compose 把 F27 的隧道值映射成容器自己的名字；部署脚本把持有的锁交给子进程 | 不开放，随 F27 |
| F37 部署脚本内部标记（2） | 脚本给自己启动的脚本设置 | 不开放，属于代理内部 |
| F38 系统与 Docker 客户端（13） | 操作系统、Docker、sshd | 不开放。`COMPOSE_PROFILES`＊例外：示例里用它启用 pgAdmin 和 edge，目标是语义开关（应用，WP7） |
| F39 第三方镜像与 SDK（12） | AWS SDK 的凭据链；PostgreSQL 和 pgAdmin 镜像自己的变量 | `AWS_*`＊：对象，归档访问密钥今天已能加密保存在 runner policy 里。`POSTGRES_*`、`PGADMIN_*`：不开放，由 F24、F21 派生 |
| F40 Workspace Broker 旧名（9） | 兼容期内仍映射 | 观测：代理报告部署存储里还有的旧名，提示清理；兼容期结束后随分类表删除 |
| F41 不再读取的旧名（1） | `sandbox_host.sh` 只提醒删除 | 观测，同上 |
| F42 主机脚本的测试接缝（4） | 测试 | 不开放 |
| F43 本地开发栈（19） | `deploy/local` | 不开放 |
| F44 仓库脚本、CI、浏览器测试（24） | 工具 | 不开放 |

## 8. 控制、执行与恢复的边界（提议）

### 8.1 三个部分

- **控制**：前端和控制面 API。记录意图、授权、审计、显示状态。控制面不执行部署命令，不打开 shell，
  不持有部署密钥的明文。
- **执行**：每台机器上的部署代理（新），只执行一个封闭的操作集合（§8.6）。托管 compute 主机保留现有的
  SSH 执行器（`SshHostBootstrap`），它已经能在控制面的发布下部署和升级主机。
- **引导存储**：代理所在机器的 `DEPLOY_ROOT`。今天它已经保存 `.env`、`.deployed-sha` 和 `.shared-live-ready`；
  代理在这里增加配置版本历史、本地策略和已验证的发布锁。它属于恢复集合（`docs/developer/disaster-recovery.md`）。

### 8.2 为什么引导值不进设置行，但仍可以在前端管理

数据库连接、设置根密钥和 `ADMIN_TOKEN` 在设置行可读之前就要用。放进设置行会形成循环依赖：数据库或解密出故障时，
修复它所需的值恰好在故障的存储里。部署代理的存储在数据库之外，所以前端可以编辑这些值（以密文形式经控制面转交给代理），
而值的权威副本和恢复路径都不依赖数据库。

### 8.3 冷启动

还没有控制面时，没有前端可用。代理的本地命令行从发布配方初始化 `DEPLOY_ROOT`（包括今天 `deploy.sh`
生成计算密钥这样的首次生成），按发布说明的顺序执行 prepare → prepare-database → 主机 activate → 控制面 activate。
控制面起来之后，代理用一次性的加入凭据注册，此后它的状态和操作出现在前端。

### 8.4 失去主 UI 或控制面

- 代理继续按最后一次应用的配置运行；没有控制面就没有新意图。
- 机器上的运维者用代理的本地命令行查看状态、回到上一配置版本，或按恢复手册用已验证的上一发布回滚。
  这里的授权来自机器上的本地权限，不需要 `ADMIN_TOKEN`。
- 控制面恢复后，代理上报期间的本地操作；控制面把它们记为「本机执行」。期望状态与实际不一致时显示漂移，
  由管理员确认采纳或重新下发。代理不自动撤销本地的恢复操作。
- 数据库丢失时按恢复手册从恢复集合恢复；恢复集合包含代理存储。

### 8.5 控制面更新自己

前端下发期望发布后，控制面机器上的代理执行分阶段部署。activate 阶段 API 会重启，页面显示「控制面正在切换」并重连。
代理在本地持久化操作记录，新控制面起来后上报结果。就绪围栏不通过时 `deploy.sh` 不切换公共服务，旧控制面继续服务，
代理通过它上报失败。

### 8.6 安全边界

- **拉取**：代理用自己的凭据向控制面取发给自己的意图并上报结果；控制面不能主动连代理执行命令。
  代理凭据不是 `ADMIN_TOKEN`，也不是主机的 runner 凭据：runner 凭据泄露不应该带来部署权限。
- **封闭操作集**：应用配置版本、重启指定服务、发布的各阶段、安装指定的 Kata 版本—摘要对、轮换和换库流程的某一步。
  参数有类型，由代理再校验一次；不提供任意命令、任意路径或任意 Compose 内容。
- **本地策略**：允许的发布来源（已验证的锁）、允许的 Kata 对、沙箱运行时和网络取值的允许集合、路径根、剖析上限。
  策略文件只能在机器上修改，前端只能读。
- **密封的值**：代理公布一把公钥；前端在浏览器里用它加密数据库口令等值，控制面只存密文和审计记录。
- **授权**：意图由经过 Gateway 的管理员发出，危险操作要求再次确认。`ADMIN_TOKEN` 今天通过 `verify_admin_access`
  授予完整管理员权限；它能否发出部署意图是 WP9 要决定的问题，在此之前按「能」评估风险。

## 9. 统一的 Admin 体验（提议）

### 9.1 一个管理目录

前端看到的是语义项，不是变量。每个语义项有：功能分组、管理方式（§4.2）、每个含义的当前来源、生效方式，以及状态。
目录由后端提供（WP3），前端只按目录渲染；未知的项有可见的默认位置。

### 9.2 状态必须如实

- 编辑项：已保存（revision）→ 已采纳（各实例或主机回报的 revision）。没有回报的实例显示「未知」，过期的心跳不算已采纳。
- 应用项：期望（配置版本）→ 进行中 → 已应用或失败（代理报告和健康检查）。
- 观测项：只显示证据，例如每台主机回报的提交、`sandbox_runtime` 和能力。
- 多含义的项按含义分别显示（例如会话签名密钥一行，根密钥回退一行）。

### 9.3 页面位置，以及与已移除卡片的区别

- Settings：按功能分组的编辑项和它们的生效状态（不变的分组原则，不用 Advanced 折叠）。
- Servers：主机设置、槽位、加入、每台主机的部署配置和 Kata 版本。
- 新的「部署」区：期望发布、每台机器的代理状态和操作记录、控制面的部署配置、轮换与数据库流程。

#1034 移除的 Deployment 卡片是静态说明加文档链接，没有状态也没有动作。「部署」区只列出已接入代理的机器和真正能执行的操作，
每个操作有记录和结果；没有接入任何代理时不出现这个区，也不恢复说明卡片。

## 10. 可以复用现有机制的部分

原来的 2 个 `candidate` 和 3 个 `design` 都可以变成普通编辑项，各自需要的机制如下。它们是 WP1、WP2 的内容，
也是第一批能交付的前端管理项，但不是可前端管理的全集。

| 变量 | 目标 | 前提 | 验收 |
|---|---|---|---|
| `AGENT_MAX_STREAMS_PER_USER` | `MIGRATED_SETTINGS` 新增整数设置（默认 24） | 门禁已要求进 `MIGRATED_SETTINGS`；`max_streams_per_user()` 改为读设置（它本来就在每次占用时现读）；上限必须包住部署里可能已有的值（§5.1）；描述写明是每个进程的上限；更新 ENVIRONMENT.md 这一行及中文翻译 | 已有数据库加 env=40 升级后，数据库值为 40；在 Admin 改成 10，约 5 秒同步后第 11 个流返回 429；之后改环境变量不再生效 |
| `AGENT_LEASE_TTL` | `HOST_SETTINGS` 新增 runner 范围的整数秒设置 | runner 每次认领使用生效值，而不是启动时的 argparse 默认值，并把它传给每个回合的子进程；下限大于一次心跳间隔加一次请求超时（30 + 10 秒），上限不超过控制面的 900 秒；旧 runner 不认识新键，`admin_values` 会丢弃它，继续用环境值，页面按 `settings_local` 显示 | 新 runner 认领请求里的 `lease_ttl_seconds` 等于管理员值；旧 runner 显示仍在用本地值 |
| `AGENT_RUNTIME_REGISTRY_URL` | 一个字符串设置，经主机设置下发 | `HostSetting` 支持字符串值；主机回报正在用的注册表；切换时主机先采纳、控制面后使用 | 改值后所有在线主机回报新地址，控制面才用新地址列版本；旧主机显示仍用本地值，控制面不切换 |
| `AGENT_MODEL_CONNECTIONS_ENABLED` | 注册为设置，打开受实例证据约束 | WP4 的实例报告能证明所有在线控制面实例都支持模型连接；混合版本安全、`application_name`、SQL 触发器是这一项设计的输入 | 有不支持的实例在线时，打开被拒绝并说明是哪个实例 |
| `AGENT_API_DOCS` | 注册为设置，文档路由按请求判断 | 路由在 `create_app()` 时总是注册，请求时按快照决定是否返回 | 打开后约 5 秒内文档可访问，关闭后返回 404，不重启 |

已注册设置上的其他事项归入相应的工作包：`control_signing_secret` 的双密钥轮换和 sandbox manager 令牌的双令牌窗口
在 WP10；`AGENT_TRUST_PROXY_HEADERS`、`AGENT_SESSION_COOKIE_SECURE`、`AGENT_DEFAULT_EXECUTION_PROFILE`
是否需要部署上限或写入、读取两侧校验，在 WP3 标注生效方式时一并评估。

## 11. 对上一稿的修正

### 11.1 本次（第二次修订，用户纠正产品目标）

1. **分类的含义**：上一版的「去向」既表示当前来源又表示应然归属。现在只表示当前来源；目标方式另成一轴（§4）。
2. **结论**：撤回「现在就能按常规方式搬进 Admin 的只有两项」「应当留在部署环境」，以及沙箱边界族理由里的
   「永不成为 admin 设置」。现在的结论是 285 个名字对应某种前端管理方式，84 个不开放（§1.2）。
3. **理由的写法**：需要重启、是密钥、启动时使用，不再被当作不能前端管理的理由（§0）。
4. **`ADMIN_TOKEN`**：上一版称它为「恢复用的 `ADMIN_TOKEN`」，容易读成窄范围令牌。它今天授予完整管理员权限（§5.2）。
5. **新增**：两轴映射（§7）、控制与恢复边界（§8）、统一 Admin 体验（§9）和实施计划。

### 11.2 第一次修订（相对最初草稿）

1. **性质**：最初草稿只是文档和门禁草案，没有实现 CI；本 PR 才实现。UI 方面的后续工作已在 #1034 实现
   （设置页简化、移除 Deployment 卡片），与本 PR 无关，本 PR 不改前端。
2. **计数**：草稿把「64 项设置」「38 项未注册」「74 个漏掉的读取点」放在一起；后者混合了名字和读取点。
   本文按唯一名和读取记录分开计数（§3）。`SETTINGS` 116 项、其中 64 项带环境变量名、`HOST_SETTINGS` 23 项，
   这几个数复核无误；ENVIRONMENT.md 表格首列共 219 个记号，也与草稿一致。
3. **pgAdmin**：保存的 upstream 不在部署允许的服务里时返回 503（§5.2），去掉自定义允许项不会变成开放代理。
4. **`control_signing_secret`**：草稿把「不在锁定集合里」列为缺口并建议锁定；它的可替换性和后果在描述里写明了，
   是否改成双密钥轮换放在 WP10。
5. **`AGENT_SESSION_SECRET`**：草稿说按旧手册改环境变量会让「控制面起不来」，这只在没有专用根密钥、
   正在使用回退时成立（§5.2）。
6. **「只进 `SETTINGS` 的新注册不会导入」**：草稿是推断，本文用代码和 git 历史确认，并找到 8 个实例（§5.1）。
7. **分类表的位置**：草稿建议放进 `cloud_agent_shared` 随两个镜像发布；本 PR 放在 `tests/support/`，不进镜像。
8. **迁入方式**：草稿建议用 `xfail(strict=True)` 挂起已知违规；本 PR 改用精确例外表加「例外仍被需要」检查，
   不挂起任何门禁。
9. **几项归类**：`AGENT_GATEWAY_SSH_HOST` 不需要退役，它和 `AGENT_CONTROL_PLANE_SSH_HOST` 一样只是表单预填值，
   保存的请求优先；`AGENT_TRUST_PROXY_HEADERS` 维持已注册状态，不做反向迁移；`AGENT_ACTION_UID` 只被写入，
   没有 Python 读者，只在文档里出现。
10. **草稿的 G11（数据库会话打标签）和 §5.3 的 P1–P6**：只作为 `MODEL_CONNECTIONS_ENABLED`
    设计的输入记录（§10），不是其他迁移的前提。

## 12. 发现的缺口与未决问题

已知欠账（已核实，未修）：

- **网页搜索的四个变量**（`WEB_SEARCH_PROVIDER`、`SERPER_API_KEY`、`BRAVE_SEARCH_API_KEY`、
  `PARALLEL_API_KEY`）已经注册，但 ENVIRONMENT.md 没有对应行，受支持的控制面 Compose 也不传它们。
  按受支持的 Compose 部署时，首次导入只能通过 override 文件发生；它们又不在 `MIGRATED_SETTINGS` 里。
  现有的 `test_environment_inventory.py` 只检查 `AGENT_`/`GATEWAY_`/`IDENTITY_` 前缀，所以没有发现。WP1 处理。
- **§5.1 的 8 个导入欠账**：哪些部署设置过这些变量、生产库的设置行建于何时，本 PR 没有查看生产配置。
- **主机能力检查用本地 `AGENT_SHARED_RESIDENT_CAPACITY`**（§5.3）：WP2 处理。
- **托管主机的 sandbox manager 令牌是设置时的快照**（§5.2）：WP10 处理。
- **只在文档里出现的名字**（15 个）：已退役的 `AGENT_EGRESS_*`、`AGENT_SANDBOX_NETWORK`、
  `AGENT_SANDBOX_ALLOW_OPEN_NETWORK`、`AGENT_MCP_CONFIG`；Gateway 侧的 `IDENTITY_*`；由
  `test-db-local.sh` 自己设置、只在测试里读的 `TEST_DB_NAME`；只被写入的 `AGENT_ACTION_UID`。
  门禁只管被读取或在模板里出现的名字，这些不进分类表。
- **shell 输入的文档覆盖**：只出现在 shell 里的 36 个输入中，19 个在 ENVIRONMENT.md、`deploy/release/README.md`、
  `deploy/local/README.md` 和 `docs/developer/` 里都找不到（`DEPLOY_SHA`、`SOURCE_REPO`、`ALLOW_DIRTY`、
  `ALLOW_STRAY_STACK`、`APP_DIR`、`HEALTH_TIMEOUT`、七个 `KATA_*`、`TEST_DB_LOCAL_*` 四个、`TEST_DB_WORKERS`、
  `UV_RUN`），只在脚本开头的注释或用法说明里写着。部署代理（WP6）把其中的部署输入变成有类型的操作参数时一并补文档。
- **ENVIRONMENT.md「Adding a variable」一节**还没有提到分类表。门禁失败信息会直接指出要改哪个文件；
  是否把这一步写进指南（需要同步中文翻译），留给维护者决定。

未决问题（提议部分）：

1. 托管 compute 主机是否也装部署代理，还是继续只用控制面的 SSH 执行器。本文建议后者，避免两条执行路径争同一台机器。
2. `ADMIN_TOKEN` 是否可以发出部署意图（§8.6）。
3. 部署代理从哪里取已验证的发布锁：下游发行版今天自己保存锁（`deploy/release/README.md`）；代理是读同一份锁，
   还是由运维者在机器上导入。
4. 控制面是否会有多台机器。若有，WP11 的逐实例重启和 WP4 的实例集合要按机器分组。
5. 控制面那份 `AGENT_EGRESS_PROTECTED_CIDRS` 是否改为从主机回报推导（§5.2）。

## 附录 A：逐名当前分类与目标方式

由门禁扫描生成（基线 `4b7b826d` 加本分支），本次修订只加了「族」和「目标方式」两列，行和「出现位置」未变。
「族」是 §7 的编号；「目标方式」见 §4.2，带＊的与所属族的主要方式不同，原因在 §7 对应行；名字后的 † 表示多含义，见 §5.2。
`sh` 后面是消费它的 shell 脚本。「出现位置」里 `py` 后面是读取它的包（`cp` 控制面、`host`、
`shared`、`sandbox-helper` 沙箱镜像里的 Python 辅助程序、`deploy-py`、`scripts`、`migrations`）和读法；
其余是模板类型和所在部署目录（`cp` 为 `deploy/control-plane`，`host` 为 `deploy/host`，`local` 为本地开发栈，
`sandbox` 为沙箱镜像，`web` 为前端）。「仅注册表」表示只在 `SETTINGS` 里出现。

| 名称 | 当前分类 | 族 | 目标方式 | 出现位置 |
|---|---|---|---|---|
| `AGENT_ALERT_WEBHOOK_URL` | admin | F05 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_CLOUDLAB_API_URL`† | admin | F08 | 编辑 | compose cp; container cp; py cp/raw |
| `AGENT_COMPUTE_REGISTRATION_URL` | admin | F08 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_COMPUTE_SECRET_KEY`† | admin | F02 | 流程 | compose cp; container cp; example cp; py cp/resolver; sh deploy.sh |
| `AGENT_CONTROL_TOKEN_SECRET` | admin | F02 | 流程 | compose cp; container cp; example local; py cp/resolver |
| `AGENT_DEFAULT_EXECUTION_PROFILE` | admin | F10 | 编辑 | compose cp; container cp; container local; py cp/resolver |
| `AGENT_DEFAULT_MODEL` | admin | F10 | 编辑 | compose cp; container cp |
| `AGENT_GITHUB_API` | admin | F03 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_GITHUB_APP_CLIENT_ID` | admin | F03 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_GITHUB_APP_CLIENT_SECRET` | admin | F03 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_GITHUB_APP_ID` | admin | F03 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_GITHUB_APP_INSTALL_URL` | admin | F03 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_GITHUB_APP_PRIVATE_KEY`† | admin | F03 | 编辑 | py cp/resolver |
| `AGENT_GITHUB_TOKEN` | admin | F03 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_GITLAB_OAUTH_CLIENT_ID` | admin | F03 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_GITLAB_OAUTH_CLIENT_SECRET` | admin | F03 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_GITLAB_OAUTH_REDIRECT_URI` | admin | F03 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_GIT_AUTHOR_EMAIL` | admin | F03 | 编辑 | compose cp; compose host; container cp; container host; example host; py cp/resolver |
| `AGENT_GIT_AUTHOR_NAME` | admin | F03 | 编辑 | compose cp; compose host; container cp; container host; example host; py cp/resolver |
| `AGENT_LOG_FORMAT`† | admin | F05 | 编辑 | compose cp; compose host; container cp; container host; py cp/raw; py host/raw |
| `AGENT_LOG_LEVEL`† | admin | F05 | 编辑 | compose cp; compose host; container cp; container host; py cp/raw; py host/raw |
| `AGENT_MCP_ALLOW_PLAIN_HTTP` | admin | F04 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_MCP_ALLOW_PRIVATE_TARGETS` | admin | F04 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_MCP_GITHUB_CLIENT_ID` | admin | F04 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_MCP_GITHUB_CLIENT_SECRET` | admin | F04 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_MCP_PUBLIC_BASE_URL` | admin | F04 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_ONCALL_DISPATCH_TOKEN` | admin | F05 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_ONCALL_USER_ID` | admin | F05 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_PGADMIN_PUBLIC_URL` | admin | F09 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_PGADMIN_URL`† | admin | F09 | 编辑 | compose cp; container cp; py cp/raw; py cp/resolver |
| `AGENT_PUBLISH_BASE_BRANCH` | admin | F03 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_REPO_ALLOWLIST` | admin | F03 | 编辑 | compose cp; container cp; example local; py cp/resolver |
| `AGENT_RESIDENCY_TTL_S` | admin | F05 | 编辑 | compose cp; container cp |
| `AGENT_SANDBOX_ARCHIVE_S3_BUCKET` | admin | F09 | 编辑 | compose cp; container cp; example cp |
| `AGENT_SANDBOX_ARCHIVE_S3_ENDPOINT` | admin | F09 | 编辑 | compose cp; container cp; example cp |
| `AGENT_SANDBOX_ARCHIVE_S3_PREFIX` | admin | F09 | 编辑 | compose cp; container cp; example cp |
| `AGENT_SANDBOX_ARCHIVE_S3_REGION` | admin | F09 | 编辑 | compose cp; container cp; example cp |
| `AGENT_SANDBOX_MANAGER_TOKEN`† | admin | F10 | 流程＊ | compose cp; compose host; compose local; container cp; container host; container local; example cp; example host; example local; py cp/resolver; py host/compat; sh sandbox_host.sh |
| `AGENT_SANDBOX_MANAGER_URL_ALLOWED_HOSTS` | admin | F10 | 编辑 | compose cp; container cp; example local; py cp/resolver |
| `AGENT_SESSION_COOKIE_SECURE` | admin | F01 | 编辑 | compose cp; container cp; example local; py cp/resolver |
| `AGENT_SESSION_REDIRECT_URI` | admin | F01 | 编辑 | compose cp; container cp; example local; py cp/resolver; sh dev.sh |
| `AGENT_SESSION_SECRET`† | admin | F02 | 流程 | compose cp; container cp; example local; py cp/raw; py cp/resolver |
| `AGENT_SOURCE_CONTROL_ENCRYPTION_KEY` | admin | F02 | 流程 | compose cp; container cp; example local; py cp/resolver |
| `AGENT_TRUST_PROXY_HEADERS` | admin | F01 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_WEB_APP_NAME` | admin | F06 | 编辑 | compose cp; container cp; example local; web web |
| `AGENT_WEB_BASE_URL` | admin | F01 | 编辑 | compose cp; container cp; example local; py cp/resolver; sh dev.sh |
| `AGENT_WEB_CONTACT_URL` | admin | F06 | 编辑 | compose cp; container cp; web web |
| `AGENT_WEB_DOCS_URL` | admin | F06 | 编辑 | compose cp; container cp; web web |
| `AGENT_WEB_FEATURE_RAG` | admin | F06 | 编辑 | compose cp; container cp; web web |
| `AGENT_WEB_GATEWAY_URL` | admin | F06 | 编辑 | compose cp; container cp; example local; web web |
| `AGENT_WEB_ORG_NAME` | admin | F06 | 编辑 | compose cp; container cp; web web |
| `AGENT_WEB_ORG_URL` | admin | F06 | 编辑 | compose cp; container cp; web web |
| `AGENT_WEB_STATUS_URL` | admin | F06 | 编辑 | compose cp; container cp; web web |
| `BRAVE_SEARCH_API_KEY` | admin | F07 | 编辑 | 仅注册表 |
| `GATEWAY_AUTHORIZE_URL` | admin | F01 | 编辑 | compose cp; container cp; example local; py cp/resolver; sh dev.sh |
| `GATEWAY_BASE_URL` | admin | F01 | 编辑 | compose cp; container cp; example local; py cp/resolver |
| `GATEWAY_GRANT_BASE_PATH` | admin | F01 | 编辑 | compose cp; container cp; py cp/resolver |
| `GATEWAY_GRANT_BINDING_ID` | admin | F01 | 编辑 | compose cp; container cp; py cp/resolver |
| `GATEWAY_GRANT_DISPATCH_TOKEN`† | admin | F01 | 编辑 | compose cp; compose local; container cp; example local; py cp/resolver; py deploy-py/raw |
| `GATEWAY_ISSUER` | admin | F01 | 编辑 | compose cp; container cp; py cp/resolver |
| `GATEWAY_JWKS_URL` | admin | F01 | 编辑 | compose cp; container cp; example local; py cp/resolver |
| `PARALLEL_API_KEY` | admin | F07 | 编辑 | 仅注册表 |
| `SERPER_API_KEY` | admin | F07 | 编辑 | 仅注册表 |
| `WEB_SEARCH_PROVIDER` | admin | F07 | 编辑 | 仅注册表 |
| `AGENT_CHAT_IDLE_TIMEOUT_S` | host_admin | F11 | 编辑 | compose host; container host; container local; example host; py host/raw; sh sandbox_host.sh |
| `AGENT_HOST_SLOTS` | host_admin | F13 | 编辑 | compose host; container host; container local; example cp; example host; py host/raw; sh sandbox_host.sh |
| `AGENT_SANDBOX_CPU_MILLICORES` | host_admin | F11 | 编辑 | compose host; container host; example host; py host/raw |
| `AGENT_SANDBOX_DISK_MIB` | host_admin | F11 | 编辑 | compose host; container host; example host; py host/raw |
| `AGENT_SANDBOX_MEMORY_MIB` | host_admin | F11 | 编辑 | compose host; container host; example host; py host/raw |
| `AGENT_SHARED_RESIDENT_CAPACITY` | host_admin | F11 | 编辑 | compose host; container host; container local; example cp; example host; py host/raw; sh sandbox_host.sh |
| `AGENT_SOURCE_IMPORT_MAX_MIB` | host_admin | F11 | 编辑 | compose host; container host; example host; py host/raw; sh sandbox_host.sh |
| `AGENT_STORAGE_MIN_AVAILABLE_MIB` | host_admin | F11 | 编辑 | py host/raw |
| `AGENT_TERMINAL_BUFFER_MAX_EVENTS` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_TERMINAL_BUFFER_MAX_MIB` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_TERMINAL_HARD_TTL_SECONDS` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_TERMINAL_IDLE_TTL_SECONDS` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_TERMINAL_INPUT_MAX_KIB` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_TERMINAL_MAX_SESSIONS` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_TERMINAL_MAX_SESSIONS_PER_WORKSPACE` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_TERMINAL_MAX_STREAMS_PER_SESSION` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_TERMINAL_OUTPUT_MAX_MIB` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_TIMEOUT_S` | host_admin | F11 | 编辑 | compose host; container host; container local; py host/raw |
| `AGENT_WORKSPACE_COMMAND_OUTPUT_MAX_MIB` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_WORKSPACE_COMMAND_TIMEOUT_S` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_WORKSPACE_DIRECTORY_MAX_ENTRIES` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_WORKSPACE_DOWNLOAD_MAX_MIB` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_WORKSPACE_FILE_PREVIEW_MAX_KIB` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_WORKSPACE_FILE_WRITE_MAX_MIB` | host_admin | F12 | 编辑 | py host/raw |
| `AGENT_LEASE_TTL` | candidate | F15 | 编辑 | compose host; container host; container local; py host/raw |
| `AGENT_MAX_STREAMS_PER_USER` | candidate | F14 | 编辑 | compose cp; container cp; py cp/raw |
| `AGENT_API_DOCS` | design | F18 | 编辑 | compose cp; container cp; py cp/raw |
| `AGENT_MODEL_CONNECTIONS_ENABLED` | design | F17 | 编辑 | compose cp; container cp; py cp/resolver |
| `AGENT_RUNTIME_REGISTRY_URL`† | design | F16 | 编辑 | compose cp; compose host; container cp; container host; example host; py cp/raw; py host/raw |
| `ADMIN_TOKEN`† | deployment | F19 | 流程 | compose cp; container cp; example cp; py cp/raw |
| `AGENT_COMPUTE_CONFIG` | deployment | F20 | 对象 | compose cp; container cp; example cp; py cp/raw |
| `AGENT_COMPUTE_DIRECTORY` | deployment | F20 | 对象 | compose cp; example cp; sh deploy.sh |
| `AGENT_COMPUTE_SANDBOX_IMAGE_REPOSITORY` | deployment | F22 | 观测 | compose cp; container cp; py cp/raw |
| `AGENT_CONTAINERD_CONTENT_DIR` | deployment | F27 | 应用 | container host; py host/raw |
| `AGENT_CONTROL_PLANE_BIND` | deployment | F24 | 应用 | compose cp |
| `AGENT_CONTROL_PLANE_COMPOSE_OVERRIDE_FILE` | deployment | F24 | 观测＊ | example cp; sh deploy.sh |
| `AGENT_CONTROL_PLANE_HOSTNAME` | deployment | F27 | 应用 | compose host; sh sandbox_host.sh |
| `AGENT_CONTROL_PLANE_PORT` | deployment | F24 | 应用 | compose cp; example local; sh deploy.sh |
| `AGENT_CONTROL_PLANE_SSH_HOST` | deployment | F23 | 对象 | compose cp; container cp; example cp; py cp/raw; sh deploy.sh |
| `AGENT_CONTROL_PLANE_TUNNEL_LISTEN_PORT` | deployment | F27 | 应用 | compose host |
| `AGENT_CONTROL_PLANE_TUNNEL_REMOTE_HOST` | deployment | F27 | 应用 | compose host; example cp |
| `AGENT_CONTROL_PLANE_TUNNEL_REMOTE_PORT` | deployment | F27 | 应用 | compose host; example cp |
| `AGENT_CONTROL_PLANE_TUNNEL_SSH_DESTINATION` | deployment | F27 | 应用 | compose host; example cp; example host; sh sandbox_host.sh |
| `AGENT_CONTROL_PLANE_TUNNEL_SSH_KEY` | deployment | F27 | 流程＊ | compose host; example cp; example host; sh sandbox_host.sh |
| `AGENT_CONTROL_PLANE_TUNNEL_SSH_KNOWN_HOSTS` | deployment | F27 | 流程＊ | compose host; example cp; example host; sh sandbox_host.sh |
| `AGENT_CONTROL_PLANE_TUNNEL_SSH_PORT` | deployment | F27 | 应用 | compose host |
| `AGENT_CONTROL_PLANE_URL` | deployment | F25 | 应用 | compose host; container cp; container host; container local; py host/raw; web web |
| `AGENT_DATABASE_URL`† | deployment | F19 | 流程 | compose cp; container cp; example cp; example local; py cp/raw; py migrations/raw |
| `AGENT_DEPLOYMENT_SHA`† | deployment | F22 | 观测 | compose cp; compose host; compose local; container cp; container host; container local; example cp; example local; py cp/raw; py host/raw; sh artifact_env.sh; sh sandbox_host.sh; web web |
| `AGENT_DEPLOY_DRAINING_HOST` | deployment | F31 | 应用 | sh deploy.sh |
| `AGENT_DEPLOY_MODE` | deployment | F31 | 应用 | sh deploy.sh; sh sandbox_host.sh |
| `AGENT_DEPLOY_PYTHON` | deployment | F31 | 观测＊ | sh artifact_env.sh |
| `AGENT_EDGE_BIND` | deployment | F24 | 应用 | compose cp; example cp |
| `AGENT_EDGE_CONFIG_FILE` | deployment | F24 | 应用 | compose cp |
| `AGENT_EDGE_PORT` | deployment | F24 | 应用 | compose cp; example cp; sh deploy.sh |
| `AGENT_EGRESS_CHAT_NETWORK_POOL` | deployment | F28 | 应用 | compose host; container host; container local; example cp; example host; py shared/raw; sh sandbox_host.sh |
| `AGENT_EGRESS_DNS_SERVER` | deployment | F28 | 应用 | compose host; container host; container local; example cp; example host; py shared/raw; sh sandbox_host.sh |
| `AGENT_EGRESS_PROTECTED_CIDRS`† | deployment | F28 | 应用 | compose cp; compose host; container cp; container host; container local; example cp; example host; py cp/raw; py shared/raw; sh sandbox_host.sh |
| `AGENT_ENROLL_TOKEN` | deployment | F26 | 对象 | compose host; container host; container local; example host; py host/raw; sh sandbox_host.sh |
| `AGENT_GATEWAY_HOSTNAME` | deployment | F27 | 应用 | compose host; sh sandbox_host.sh |
| `AGENT_GATEWAY_SSH_HOST` | deployment | F23 | 对象 | compose cp; container cp; example cp; py cp/raw |
| `AGENT_GATEWAY_URL` | deployment | F27 | 应用 | container host; container local; py host/raw; py shared/raw |
| `AGENT_GITHUB_APP_PRIVATE_KEY_FILE` | deployment | F20 | 对象 | compose cp; example cp |
| `AGENT_GITHUB_APP_PRIVATE_KEY_PATH`† | deployment | F20 | 对象 | compose cp; container cp; container local; py cp/raw; py cp/resolver |
| `AGENT_HOST_CAPACITY_ENABLED` | deployment | F27 | 应用 | compose host; container host; example host; py host/raw |
| `AGENT_HOST_CONTAINERD_CONTENT_DIR` | deployment | F27 | 应用 | compose host; example host |
| `AGENT_HOST_CREDENTIAL` | deployment | F26 | 对象 | py host/raw |
| `AGENT_HOST_CREDENTIAL_FILE` | deployment | F26 | 对象 | container host; container local; py host/raw |
| `AGENT_HOST_STATE_ROOT` | deployment | F27 | 应用 | compose host; example host; sh sandbox_host.sh |
| `AGENT_HOST_STORAGE_PROBE` | deployment | F27 | 应用 | compose host; container host; example cp; example host; py host/raw; sh sandbox_host.sh |
| `AGENT_MCP_HOSTNAME` | deployment | F27 | 应用 | compose host; sh sandbox_host.sh |
| `AGENT_MCP_RELAY_BIND` | deployment | F24 | 应用 | compose cp |
| `AGENT_MCP_RELAY_PORT` | deployment | F24 | 应用 | compose cp; example local; sh deploy.sh |
| `AGENT_MCP_TUNNEL_LISTEN_PORT` | deployment | F27 | 应用 | compose host; sh sandbox_host.sh |
| `AGENT_MCP_TUNNEL_REMOTE_HOST` | deployment | F27 | 应用 | compose host; example cp |
| `AGENT_MCP_TUNNEL_REMOTE_PORT` | deployment | F27 | 应用 | compose host; example cp |
| `AGENT_MCP_TUNNEL_SSH_DESTINATION` | deployment | F27 | 应用 | compose host; example cp; example host; sh sandbox_host.sh |
| `AGENT_MCP_TUNNEL_SSH_KEY` | deployment | F27 | 流程＊ | compose host; example cp; example host; sh sandbox_host.sh |
| `AGENT_MCP_TUNNEL_SSH_KNOWN_HOSTS` | deployment | F27 | 流程＊ | compose host; example cp; example host; sh sandbox_host.sh |
| `AGENT_MCP_TUNNEL_SSH_PORT` | deployment | F27 | 应用 | compose host |
| `AGENT_NETWORK_NAME` | deployment | F24 | 应用 | compose cp; example cp; example local; sh dev.sh |
| `AGENT_OPERATION_PROFILING_DEPLOYMENT` | deployment | F24 | 应用 | compose cp; compose local; container cp; container local; example cp; py cp/raw |
| `AGENT_OPERATION_PROFILING_ENABLED` | deployment | F29 | 应用 | compose cp; compose host; compose local; container cp; container host; container local; example cp; example host; py shared/raw |
| `AGENT_OPERATION_PROFILING_MODE` | deployment | F29 | 应用 | compose cp; compose host; compose local; container cp; container host; container local; example cp; example host; py shared/raw |
| `AGENT_OPERATION_PROFILING_RUN` | deployment | F29 | 应用 | compose cp; compose host; compose local; container cp; container host; container local; example cp; example host; py shared/raw |
| `AGENT_OPERATION_PROFILING_SINK` | deployment | F29 | 应用 | compose cp; compose host; compose local; container cp; container host; container local; example cp; example host; py host/raw; py shared/raw |
| `AGENT_PGADMIN_BOOTSTRAP_DIR` | deployment | F21 | 应用 | compose cp; container cp; example cp; py cp/raw |
| `AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL` | deployment | F21 | 应用 | compose cp; container cp; example cp; py cp/raw |
| `AGENT_PGADMIN_BOOTSTRAP_URL` | deployment | F21 | 应用 | compose cp; container cp; example cp; py cp/raw |
| `AGENT_PGADMIN_COOKIE_SECURE` | deployment | F21 | 应用 | compose cp; example cp |
| `AGENT_PGADMIN_ENTRYPOINT_FILE` | deployment | F21 | 应用 | compose cp; example cp; sh artifact_env.sh |
| `AGENT_PGADMIN_SERVERS_FILE` | deployment | F21 | 应用 | compose cp; example cp; sh artifact_env.sh |
| `AGENT_PHYSICAL_HOST` | deployment | F26 | 对象 | compose host; container host; example host; py host/raw; sh sandbox_host.sh |
| `AGENT_POSTGRES_DB` | deployment | F24 | 流程＊ | compose cp; compose local; example local |
| `AGENT_POSTGRES_PASSWORD` | deployment | F24 | 流程＊ | compose cp; compose local; example cp; example local |
| `AGENT_POSTGRES_USER` | deployment | F24 | 流程＊ | compose cp; compose local; example local |
| `AGENT_RELAY_NETWORK` | deployment | F28 | 应用 | compose host; container host; container local; py shared/raw; sh sandbox_host.sh |
| `AGENT_RELEASE_MANIFEST` | deployment | F31 | 应用 | sh artifact_env.sh |
| `AGENT_REPO_CLONE_BASE_URL` | deployment | F27 | 应用 | container local; py host/raw |
| `AGENT_RETAINED_DOCKER_DATA_ROOT` | deployment | F27 | 应用 | compose host; compose local; container host; container local; example cp; example host; example local; py host/raw; sh sandbox_host.sh |
| `AGENT_RUNNER_HOST` | deployment | F26 | 对象 | compose host; container host; container local; example host; py host/raw; sh sandbox_host.sh |
| `AGENT_RUNNER_STOP_GRACE` | deployment | F27 | 应用 | compose host; example host |
| `AGENT_RUNNER_VERSION` | deployment | F22 | 观测 | py host/raw |
| `AGENT_RUNTIME_DOWNLOAD_NETWORK` | deployment | F27 | 应用 | compose host; container host; example host; py host/raw |
| `AGENT_SANDBOX_ALLOW_UNISOLATED` | deployment | F28 | 观测＊ | py host/raw |
| `AGENT_SANDBOX_ARCHIVE_STATE_DIR` | deployment | F27 | 应用 | container host; py host/raw |
| `AGENT_SANDBOX_BACKEND` | deployment | F28 | 应用 | compose host; container host; container local; example host; py host/raw; sh sandbox_host.sh |
| `AGENT_SANDBOX_DOCKER` | deployment | F28 | 应用 | py host/raw |
| `AGENT_SANDBOX_EXTRA_ARGS` | deployment | F28 | 观测＊ | py host/raw |
| `AGENT_SANDBOX_GID` | deployment | F28 | 应用 | py host/raw |
| `AGENT_SANDBOX_HOME` | deployment | F28 | 应用 | py host/raw |
| `AGENT_SANDBOX_IMAGE` | deployment | F28 | 观测＊ | compose host; container host; container local; example cp; example host; py host/raw; sh sandbox_host.sh |
| `AGENT_SANDBOX_IMAGE_PULL` | deployment | F27 | 应用 | example cp; sh sandbox_host.sh |
| `AGENT_SANDBOX_MANAGER_ADVERTISE_URL` | deployment | F27 | 应用 | compose host; container host; container local; example cp; example host; py host/compat; sh sandbox_host.sh |
| `AGENT_SANDBOX_MANAGER_REVERSE_BIND` | deployment | F27 | 应用 | compose host; example cp; example host; sh sandbox_host.sh |
| `AGENT_SANDBOX_MANAGER_REVERSE_PORT` | deployment | F27 | 应用 | compose host; example cp; example host; sh sandbox_host.sh |
| `AGENT_SANDBOX_MANAGER_REVERSE_TARGET_HOST` | deployment | F27 | 应用 | compose host |
| `AGENT_SANDBOX_MANAGER_REVERSE_TARGET_PORT` | deployment | F27 | 应用 | compose host |
| `AGENT_SANDBOX_MANAGER_STOP_GRACE` | deployment | F27 | 应用 | compose host |
| `AGENT_SANDBOX_MANAGER_URL` | deployment | F27 | 应用 | container host; container local; py host/compat |
| `AGENT_SANDBOX_MCP_URL` | deployment | F27 | 应用 | container host; container local; py host/raw; py shared/raw |
| `AGENT_SANDBOX_NAMESPACE` | deployment | F28 | 应用 | container host; container local; py host/raw; py shared/raw |
| `AGENT_SANDBOX_RUNTIME` | deployment | F28 | 应用 | py host/raw |
| `AGENT_SANDBOX_UID` | deployment | F28 | 应用 | py host/raw |
| `AGENT_SHARED_LIVE_READY_FILE` | deployment | F27 | 应用 | container host; container local; py host/raw |
| `AGENT_TUNNEL_LISTEN_PORT` | deployment | F27 | 应用 | compose host; sh sandbox_host.sh |
| `AGENT_TUNNEL_REMOTE_HOST` | deployment | F27 | 应用 | compose host; example cp |
| `AGENT_TUNNEL_REMOTE_PORT` | deployment | F27 | 应用 | compose host; example cp |
| `AGENT_TUNNEL_SSH_DESTINATION` | deployment | F27 | 应用 | compose host; example cp; example host; sh sandbox_host.sh |
| `AGENT_TUNNEL_SSH_KEY` | deployment | F27 | 流程＊ | compose host; example cp; example host; sh sandbox_host.sh |
| `AGENT_TUNNEL_SSH_KNOWN_HOSTS` | deployment | F27 | 流程＊ | compose host; example cp; example host; sh sandbox_host.sh |
| `AGENT_TUNNEL_SSH_PORT` | deployment | F27 | 应用 | compose host |
| `AGENT_UPLOAD_RECEIPT_DIR` | deployment | F27 | 应用 | container host; container local; py host/raw |
| `AGENT_WEB_API_URL` | deployment | F25 | 应用 | compose cp; example cp; example local |
| `AGENT_WEB_BASE_PATH` | deployment | F25 | 应用 | compose cp; example cp; example local; sh artifact_env.sh; sh deploy.sh |
| `AGENT_WEB_BIND` | deployment | F24 | 应用 | compose cp |
| `AGENT_WEB_DEPLOYED_AT` | deployment | F22 | 观测 | compose cp; container cp; web web |
| `AGENT_WEB_PORT` | deployment | F24 | 应用 | compose cp; example local; sh deploy.sh |
| `AGENT_WEB_VARIANT` | deployment | F31 | 应用 | sh artifact_env.sh |
| `AGENT_WORKER_ID` | deployment | F26 | 对象 | container local; py host/raw |
| `ALLOW_DIRTY` | deployment | F31 | 不开放＊ | sh deploy.sh |
| `ALLOW_STRAY_STACK` | deployment | F31 | 不开放＊ | sh deploy.sh |
| `APP_DIR` | deployment | F31 | 观测＊ | sh sandbox_host.sh |
| `BUILD_SHA` | deployment | F22 | 观测 | compose cp |
| `BUILD_TIMESTAMP` | deployment | F22 | 观测 | compose cp |
| `CLAUDE_CODE_EXTRA_VERSIONS` | deployment | F30 | 观测 | dockerfile_arg sandbox; example host; sh sandbox_host.sh |
| `CLAUDE_CODE_VERSION` | deployment | F30 | 观测 | dockerfile_arg sandbox; sh sandbox_host.sh |
| `CLAUDE_CODE_VERSIONS` | deployment | F30 | 观测 | dockerfile_arg sandbox |
| `CODEX_EXTRA_VERSIONS` | deployment | F30 | 观测 | dockerfile_arg sandbox; example host; sh sandbox_host.sh |
| `CODEX_VERSION` | deployment | F30 | 观测 | dockerfile_arg sandbox; sh sandbox_host.sh |
| `CODEX_VERSIONS` | deployment | F30 | 观测 | dockerfile_arg sandbox |
| `COMPOSE_PROJECT` | deployment | F27 | 应用 | compose host; example host; sh deploy.sh; sh sandbox_host.sh |
| `DEBIAN_MIRROR` | deployment | F30 | 观测 | dockerfile_arg cp; dockerfile_arg host; dockerfile_arg local; dockerfile_arg sandbox |
| `DEPLOY_ROOT` | deployment | F31 | 观测＊ | sh deploy.sh |
| `DEPLOY_SHA` | deployment | F31 | 应用 | sh deploy.sh |
| `HEALTH_TIMEOUT` | deployment | F31 | 应用 | sh deploy.sh |
| `KATA_CONFIG_PATH` | deployment | F32 | 观测＊ | sh setup_kata_runtime.sh |
| `KATA_DEBUG_KERNEL_PATH` | deployment | F32 | 观测＊ | sh setup_kata_runtime.sh |
| `KATA_DOWNLOAD_BASE` | deployment | F32 | 观测＊ | sh setup_kata_runtime.sh |
| `KATA_LINK_DIR` | deployment | F32 | 观测＊ | sh setup_kata_runtime.sh |
| `KATA_SETUP_SCRIPT` | deployment | F32 | 观测＊ | sh sandbox_host.sh |
| `KATA_SHA256` | deployment | F32 | 应用 | sh setup_kata_runtime.sh |
| `KATA_VERSION` | deployment | F32 | 应用 | sh setup_kata_runtime.sh |
| `KILO_EXTRA_VERSIONS` | deployment | F30 | 观测 | dockerfile_arg sandbox; example host; sh sandbox_host.sh |
| `KILO_VERSION` | deployment | F30 | 观测 | dockerfile_arg sandbox; sh sandbox_host.sh |
| `KILO_VERSIONS` | deployment | F30 | 观测 | dockerfile_arg sandbox |
| `NEXT_PUBLIC_AGENT_API_URL` | deployment | F25 | 应用 | build_arg cp; dockerfile_arg cp; web web |
| `NEXT_PUBLIC_BASE_PATH` | deployment | F25 | 应用 | build_arg cp; dockerfile_arg cp; web web |
| `NEXT_PUBLIC_BUILD_SHA` | deployment | F22 | 观测 | build_arg cp; dockerfile_arg cp; web web |
| `NEXT_PUBLIC_BUILD_TIMESTAMP` | deployment | F22 | 观测 | build_arg cp; dockerfile_arg cp; web web |
| `OPENCODE_EXTRA_VERSIONS` | deployment | F30 | 观测 | dockerfile_arg sandbox; example host; sh sandbox_host.sh |
| `OPENCODE_VERSION` | deployment | F30 | 观测 | dockerfile_arg sandbox; sh sandbox_host.sh |
| `OPENCODE_VERSIONS` | deployment | F30 | 观测 | dockerfile_arg sandbox |
| `PIP_DEFAULT_TIMEOUT` | deployment | F30 | 观测 | dockerfile_arg cp; dockerfile_arg host; dockerfile_arg local; dockerfile_arg sandbox |
| `PI_EXTRA_VERSIONS` | deployment | F30 | 观测 | dockerfile_arg sandbox; example host; sh sandbox_host.sh |
| `PI_VERSION` | deployment | F30 | 观测 | dockerfile_arg sandbox; sh sandbox_host.sh |
| `PI_VERSIONS` | deployment | F30 | 观测 | dockerfile_arg sandbox |
| `SHARED_READY_TIMEOUT` | deployment | F27 | 应用 | example host; sh deploy.sh; sh sandbox_host.sh |
| `SIBLING_COMPOSE_PROJECTS` | deployment | F27 | 应用 | example host; sh deploy.sh; sh sandbox_host.sh |
| `SOURCE_COMMIT` | deployment | F22 | 观测 | build_arg cp; dockerfile_arg cp; dockerfile_arg host; dockerfile_arg sandbox |
| `SOURCE_REPO` | deployment | F31 | 应用 | sh deploy.sh |
| `UV_HTTP_TIMEOUT` | deployment | F30 | 观测 | dockerfile_arg cp; dockerfile_arg host |
| `UV_VERSION` | deployment | F30 | 观测 | dockerfile_arg cp; dockerfile_arg host |
| `AGENT_SETTINGS_ENCRYPTION_KEY` | operation | F33 | 流程 | compose cp; container cp; example cp; py cp/raw |
| `AGENT_COMPAT_CLIENT` | internal | F34 | 对象 | py host/raw |
| `AGENT_COMPAT_MODEL` | internal | F34 | 对象 | py host/raw |
| `AGENT_COMPAT_TOKEN` | internal | F34 | 对象 | py host/raw |
| `AGENT_COMPAT_UPSTREAM_KEY` | internal | F34 | 对象 | py host/raw |
| `AGENT_COMPAT_UPSTREAM_URL` | internal | F34 | 对象 | py host/raw |
| `AGENT_ENVIRONMENT_SCRIPT` | internal | F34 | 对象 | py host(embedded)/raw |
| `AGENT_LIVE_SESSION_ID` | internal | F34 | 对象 | py sandbox-helper/raw |
| `AGENT_LIVE_WORKDIR` | internal | F34 | 对象 | py sandbox-helper/raw |
| `AGENT_MCP_SERVERS` | internal | F34 | 对象 | py sandbox-helper/raw |
| `AGENT_MCP_TOKEN` | internal | F34 | 对象 | py sandbox-helper/raw |
| `AGENT_MEMBER_PROCESS_REAPER_SOURCE` | internal | F35 | 不开放 | py host/raw |
| `AGENT_PLUGIN_PATHS` | internal | F34 | 对象 | py sandbox-helper/raw |
| `AGENT_SCRIPT_STATUS` | internal | F34 | 对象 | py host(embedded)/raw |
| `AGENT_SECRET_PATTERN_*` | internal | F34 | 对象 | py host(embedded)/raw |
| `DEFER_SHARED_READY_MARKER` | internal | F37 | 不开放 | sh sandbox_host.sh |
| `KILO_FREEINFERENCE_CONFIG_DIR` | internal | F34 | 对象 | py sandbox-helper/raw |
| `KILO_GATEWAY_MODEL` | internal | F34 | 对象 | py sandbox-helper/raw |
| `KILO_GATEWAY_REASONING_EFFORT` | internal | F34 | 对象 | py sandbox-helper/raw |
| `OPENAI_API_KEY` | internal | F34 | 对象 | py sandbox-helper/raw |
| `OPENAI_BASE_URL` | internal | F34 | 对象 | py sandbox-helper/raw |
| `OPENCODE_FREEINFERENCE_CONFIG_DIR` | internal | F34 | 对象 | py sandbox-helper/raw |
| `OPENCODE_GATEWAY_MODEL` | internal | F34 | 对象 | py sandbox-helper/raw |
| `OPENCODE_GATEWAY_REASONING_EFFORT` | internal | F34 | 对象 | py sandbox-helper/raw |
| `PI_CODING_AGENT_DIR` | internal | F34 | 对象 | py sandbox-helper/raw |
| `PI_GATEWAY_MODEL` | internal | F34 | 对象 | py sandbox-helper/raw |
| `PI_GATEWAY_REASONING` | internal | F34 | 对象 | py sandbox-helper/raw |
| `TUNNEL_LISTEN_PORT` | internal | F36 | 不开放 | container host; sh agent-gateway-tunnel.sh |
| `TUNNEL_REMOTE_HOST` | internal | F36 | 不开放 | container host; sh agent-gateway-tunnel.sh |
| `TUNNEL_REMOTE_PORT` | internal | F36 | 不开放 | container host; sh agent-gateway-tunnel.sh |
| `TUNNEL_REVERSE_BIND` | internal | F36 | 不开放 | container host; sh agent-gateway-tunnel.sh |
| `TUNNEL_REVERSE_PORT` | internal | F36 | 不开放 | container host; sh agent-gateway-tunnel.sh |
| `TUNNEL_REVERSE_TARGET_HOST` | internal | F36 | 不开放 | container host; sh agent-gateway-tunnel.sh |
| `TUNNEL_REVERSE_TARGET_PORT` | internal | F36 | 不开放 | container host; sh agent-gateway-tunnel.sh |
| `TUNNEL_SSH_DESTINATION` | internal | F36 | 不开放 | container host; sh agent-gateway-tunnel.sh |
| `TUNNEL_SSH_KEY` | internal | F36 | 不开放 | sh agent-gateway-tunnel.sh |
| `TUNNEL_SSH_KNOWN_HOSTS` | internal | F36 | 不开放 | sh agent-gateway-tunnel.sh |
| `TUNNEL_SSH_PORT` | internal | F36 | 不开放 | container host; sh agent-gateway-tunnel.sh |
| `WATCH_READINESS` | internal | F37 | 不开放 | sh deploy.sh |
| `_KATA_RUNTIME_EPOCH_LOCK_FD` | internal | F36 | 不开放 | py deploy-py/raw; sh setup_kata_runtime.sh |
| `AWS_ACCESS_KEY_ID` | platform | F39 | 对象＊ | example cp |
| `AWS_DEFAULT_REGION` | platform | F39 | 对象＊ | example cp |
| `AWS_SECRET_ACCESS_KEY` | platform | F39 | 对象＊ | example cp |
| `AWS_SESSION_TOKEN` | platform | F39 | 对象＊ | example cp |
| `COMPOSE_PROFILES` | platform | F38 | 应用＊ | example cp |
| `DOCKER_CONFIG` | platform | F38 | 不开放 | py host/raw |
| `DOCKER_CONTEXT` | platform | F38 | 不开放 | py host/raw |
| `DOCKER_HOST` | platform | F38 | 不开放 | py host/raw |
| `HOME` | platform | F38 | 不开放 | py host/raw |
| `HOSTNAME` | platform | F38 | 不开放 | py host/raw |
| `LANG` | platform | F38 | 不开放 | py host/raw |
| `LC_ALL` | platform | F38 | 不开放 | py host/raw |
| `NODE_ENV` | platform | F38 | 不开放 | web web |
| `PATH` | platform | F38 | 不开放 | py cp/raw; py host/raw |
| `PGADMIN_CONFIG_PROXY_X_PROTO_COUNT` | platform | F39 | 不开放 | container cp |
| `PGADMIN_CONFIG_SERVER_MODE` | platform | F39 | 不开放 | container cp |
| `PGADMIN_CONFIG_SESSION_COOKIE_SECURE` | platform | F39 | 不开放 | container cp |
| `PGADMIN_DISABLE_POSTFIX` | platform | F39 | 不开放 | container cp |
| `PGADMIN_LISTEN_PORT` | platform | F39 | 不开放 | container cp |
| `POSTGRES_DB` | platform | F39 | 不开放 | container cp |
| `POSTGRES_PASSWORD` | platform | F39 | 不开放 | container cp |
| `POSTGRES_USER` | platform | F39 | 不开放 | container cp |
| `SSH_ORIGINAL_COMMAND` | platform | F38 | 不开放 | py cp(embedded)/raw |
| `TERM` | platform | F38 | 不开放 | py host/raw |
| `TMPDIR` | platform | F38 | 不开放 | py host/raw; sh artifact_env.sh |
| `AGENT_BROKER_REVERSE_BIND` | retired | F40 | 观测 | compose host |
| `AGENT_BROKER_REVERSE_PORT` | retired | F40 | 观测 | compose host |
| `AGENT_BROKER_REVERSE_TARGET_HOST` | retired | F40 | 观测 | compose host |
| `AGENT_BROKER_REVERSE_TARGET_PORT` | retired | F40 | 观测 | compose host |
| `AGENT_BROKER_STOP_GRACE` | retired | F40 | 观测 | compose host |
| `AGENT_BROKER_URL_ALLOWED_HOSTS` | retired | F40 | 观测 | compose cp |
| `AGENT_RUNNER_REPLICAS` | retired | F41 | 观测 | sh sandbox_host.sh |
| `AGENT_WORKSPACE_BROKER_ADVERTISE_URL` | retired | F40 | 观测 | compose host |
| `AGENT_WORKSPACE_BROKER_TOKEN` | retired | F40 | 观测 | compose cp |
| `AGENT_WORKSPACE_BROKER_URL` | retired | F40 | 观测 | 仅注册表 |
| `CAE_BENCH_DNS_SERVER` | tooling | F44 | 不开放 | py scripts/raw |
| `CI` | tooling | F44 | 不开放 | web web |
| `CI_LOG_DIR` | tooling | F44 | 不开放 | sh ci-local.sh |
| `CLOUD_AGENT_E2E_PORT` | tooling | F44 | 不开放 | web web |
| `CLOUD_AGENT_E2E_SKIP_BUILD` | tooling | F44 | 不开放 | web web |
| `CLOUD_AGENT_TEST_API_KEY_FILE` | tooling | F44 | 不开放 | py deploy-py/raw |
| `CLOUD_AGENT_TEST_PROXY` | tooling | F44 | 不开放 | py deploy-py/raw |
| `DEPLOY_PEER_JOB` | tooling | F44 | 不开放 | sh deploy.sh |
| `DOCKER_DAEMON_JSON` | tooling | F42 | 不开放 | sh sandbox_host.sh |
| `GITHUB_API_URL` | tooling | F44 | 不开放 | py deploy-py/raw |
| `GITHUB_REPOSITORY` | tooling | F44 | 不开放 | py deploy-py/raw |
| `GITHUB_RUN_ATTEMPT` | tooling | F44 | 不开放 | py deploy-py/raw; sh deploy.sh |
| `GITHUB_RUN_ID` | tooling | F44 | 不开放 | py deploy-py/raw; sh deploy.sh |
| `GITHUB_TOKEN` | tooling | F44 | 不开放 | py deploy-py/raw |
| `KATA_CPUINFO` | tooling | F42 | 不开放 | sh setup_kata_runtime.sh |
| `KATA_EXTRACT_ROOT` | tooling | F42 | 不开放 | sh sandbox_host.sh; sh setup_kata_runtime.sh |
| `KATA_KVM_DEVICE` | tooling | F42 | 不开放 | sh setup_kata_runtime.sh |
| `LEGACY_COPY_DATABASE_URL` | tooling | F44 | 不开放 | py scripts/raw |
| `LOCAL_AGENT_TIMEOUT_S` | tooling | F43 | 不开放 | compose local; example local |
| `LOCAL_CHAT_IDLE_TIMEOUT_S` | tooling | F43 | 不开放 | compose local; example local |
| `LOCAL_CHAT_NETWORK_POOL` | tooling | F43 | 不开放 | compose local; example local |
| `LOCAL_EDGE_PORT` | tooling | F43 | 不开放 | compose local; container local; example local; py deploy-py/raw; sh dev.sh |
| `LOCAL_EGRESS_DNS_SERVER` | tooling | F43 | 不开放 | compose local; example local |
| `LOCAL_EGRESS_PROTECTED_CIDRS` | tooling | F43 | 不开放 | compose local; example local |
| `LOCAL_ENROLL_TOKEN` | tooling | F43 | 不开放 | compose local; example local |
| `LOCAL_ENROLL_TOKEN_SHA256` | tooling | F43 | 不开放 | compose local; container local; example local |
| `LOCAL_GATEWAY_DISPATCH_TOKEN` | tooling | F43 | 不开放 | container local; py deploy-py/raw |
| `LOCAL_HOST_SLOTS` | tooling | F43 | 不开放 | compose local; example local |
| `LOCAL_IDENTITY_ALLOWED_REDIRECTS` | tooling | F43 | 不开放 | compose local; container local; example local; py deploy-py/raw |
| `LOCAL_PROJECT_NAME` | tooling | F43 | 不开放 | compose local; example local; sh dev.sh |
| `LOCAL_RELAY_NETWORK` | tooling | F43 | 不开放 | compose local; example local; sh dev.sh |
| `LOCAL_SANDBOX_IMAGE` | tooling | F43 | 不开放 | compose local; example local |
| `LOCAL_STACK_ID` | tooling | F43 | 不开放 | container local; py deploy-py/raw |
| `PGDATABASE` | tooling | F43 | 不开放 | container local |
| `PGHOST` | tooling | F43 | 不开放 | container local |
| `PGPASSWORD` | tooling | F43 | 不开放 | container local |
| `PGUSER` | tooling | F43 | 不开放 | container local |
| `TEST_DB_HOST` | tooling | F44 | 不开放 | py scripts/raw |
| `TEST_DB_LOCAL_CONTAINER` | tooling | F44 | 不开放 | sh test-db-local.sh |
| `TEST_DB_LOCAL_IMAGE` | tooling | F44 | 不开放 | sh test-db-local.sh |
| `TEST_DB_LOCAL_NAME` | tooling | F44 | 不开放 | sh test-db-local.sh |
| `TEST_DB_LOCAL_PORT` | tooling | F44 | 不开放 | sh test-db-local.sh |
| `TEST_DB_PASSWORD` | tooling | F44 | 不开放 | py scripts/raw |
| `TEST_DB_PORT` | tooling | F44 | 不开放 | py scripts/raw |
| `TEST_DB_USER` | tooling | F44 | 不开放 | py scripts/raw |
| `TEST_DB_WORKERS` | tooling | F44 | 不开放 | sh test-db-local.sh |
| `UV_RUN` | tooling | F44 | 不开放 | sh test-db-local.sh |
