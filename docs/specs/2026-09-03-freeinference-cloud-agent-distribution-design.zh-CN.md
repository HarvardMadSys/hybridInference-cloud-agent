# Cloud Agent 上游 / FreeInference Distribution 边界设计

- **语言：** 简体中文 · [English](2026-09-03-freeinference-cloud-agent-distribution-design.md)
- **状态：** v1 提案——待评审（Murphy、Juncheng）
- **日期：** 2026-09-03
- **适用仓库：**
  [HarvardMadSys/hybridInference-cloud-agent](https://github.com/HarvardMadSys/hybridInference-cloud-agent)
  （可复用上游）和
  [HarvardMadSys/freeInference](https://github.com/HarvardMadSys/freeInference)
  （私有 FreeInference distribution）
- **相关内容：** Cloud Agent 开源前 PR
  [#98](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/98)、
  [#99](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/99)、
  [#100](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/100) 和
  [#101](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/101)；
  FreeInference 的
  [上游消费契约](https://github.com/HarvardMadSys/freeInference/blob/dev/docs/maintainers/consuming-upstream.md)
- **取代范围：** §10 切换完成后，本设计取代当前由本仓库直接管理
  FreeInference staging 和 production 的模式。它不取代 Cloud Agent 拆仓计划、
  gateway HTTP 契约或 sandbox 安全模型。

本文件是英文设计文档的完整中文版本。两份文件在同一个提交中维护；如果翻译措辞
出现歧义，以同一提交中的英文原文及最终评审结论为准。

---

## 0. 决策

Cloud Agent 采用 HybridInference 与 FreeInference 已经使用的同类产品/平台边界：

> **公开上游只生产中立、不可变的运行时制品。私有 FreeInference 仓库通过一份
> 经过评审的 lock，选择、配置、部署、晋级并回滚这些制品。**

上游合并不得直接触达 FreeInference 主机。目标流程如下：

```text
hybridInference-cloud-agent dev
  -> 托管 CI 与安全门禁
  -> 为一个 source SHA 生成不可变 GHCR 制品
  -> 不部署

freeInference bump-cloud-agent
  -> 要求该上游 SHA 对应的 push CI 全绿
  -> 将每个必需制品解析为 digest
  -> 校验 package、platform 和 revision label
  -> 创建一个只修改 cloud-agent.lock 的 PR
  -> FreeInference CI
  -> 合入 FreeInference dev
  -> 按 lock 部署 staging
  -> 通过 main 手动晋级
  -> production 按同一份 lock 部署
```

这不只是一次品牌清理。当前仓库仍然拥有 FreeInference runner label、主机路径、
Compose project、构建期 Web 身份、带产品名的 harness wrapper，以及 staging 和
production 两套部署 workflow。如果保持这种结构就直接开源，那么公开仓库的一次
成功 push 仍可能直接抵达私有基础设施。PR runner guard 只能缩小一种攻击路径，
不能补上缺失的所有权边界。

这里的 distribution 首先是一份**仓库与部署契约**，不是运行时 Python plugin。
Cloud Agent 继续维护带类型的环境变量接口和启动校验。FreeInference 部署工具负责
校验私有 manifest 并将它渲染到该接口；Cloud Agent 不 import、也不定位私有仓库。

## 1. 问题

服务拆分已经完成了代码与数据边界：

- Cloud Agent 拥有 job、attempt、host、sandbox、源码平台集成、发布流程及自己的数据库。
- HybridInference 拥有身份认证与 inference grant 授权能力。
- 两个服务只通过 `contracts/` 中的契约通信。
- Gateway 签名密钥绝不进入 Cloud Agent。

但部署边界没有随服务边界一起完成。在当前 `dev` 上：

1. `.github/workflows/ci.yml` 在 push 到 `dev` 后运行。
2. `.github/workflows/deploy-staging.yml` 监听该 CI workflow 的成功完成事件。
3. control-plane 和 host 两个 job 都在带 FreeInference label 的 self-hosted runner 上
   运行，并部署对应源码提交。
4. `deploy/control-plane/deploy.sh` 和 `deploy/host/deploy.sh` 会 reset 托管的源码
   checkout，在主机本地构建，然后替换运行中的 stack。

因此，即使只合并一处文档变更，也会排队一次 staging 部署。四个开源准备 PR 如果
分别合并，就会排队四次部署，因为 staging deployment 的 concurrency group 明确
不会取消正在运行的部署。

production 当前虽然是手动部署，但仍由上游仓库拥有：
`.github/workflows/deploy-prod.yml` 直接写有 FreeInference runner pool、路径、
Compose identity 和环境变量。“手动”只限制了运行时间，并没有建立正确的仓库权限边界。

剩余耦合可分为六类：

| 类别 | 当前示例 | 为什么应由别处拥有 |
|---|---|---|
| 产品身份 | `FreeInference Cloud Agent`、FreeInference 链接和 OpenAPI server | 下游 distribution 应决定用户看到的身份 |
| Web 构建身份 | 由 `Dockerfile.web` 烘焙的 `NEXT_PUBLIC_APP_NAME`、组织/状态/gateway 值 | 烘焙会让 staging、production 和其他运营方得到不同字节 |
| Host 拓扑 | `deploy-cloud-agent-*-staging`、`/srv/freeinference-cloud-agent` | 它们描述私有机器及其托管 checkout |
| Stack 身份 | FreeInference Compose project、network、image tag、state root | 改动这些名字可能接管、遗弃或破坏错误的运行时状态 |
| Harness 集成身份 | `pi-freeinference`、`freeinference/<model>`、`org.freeinference.runtime.*` | 有些只是展示名，有些则是需要兼容的持久化标识符 |
| 发布权限 | 上游仓库直接 checkout、构建、部署与回滚 | 产品 distribution 必须决定运行哪一个已验证的上游版本 |

仅做字符串替换无法正确分类这些内容。产品标题可以立即迁移；持久化 provider ID 或
Docker label 需要兼容窗口；数据库表、OAuth audience 或环境变量可能是协议标识符，
而不是品牌，因而根本不应该重命名。

## 2. 目标与非目标

### 目标

1. 公开上游中的 merge、release、仓库失陷或 workflow 配置错误，都不能改变任何
   FreeInference 主机；只有私有 distribution 接受新 lock 后才可以部署。
2. staging 与 production 按 digest 消费同一个上游 source SHA 生成的不可变制品，
   并在替换前校验 provenance。
3. FreeInference 保留自己的产品名称、URL、GitHub App、运维拓扑、策略和 smoke test，
   无需 fork Cloud Agent 代码。
4. 第一次由 distribution 执行的部署必须保留现有数据库、volume、Docker data root、
   network、container ownership、已注册 host credential、retained sandbox 和原生
   session state。
5. 切换失败时，可以在不重新构建旧源码的情况下恢复上一版 distribution revision
   和 image set。
6. 第三方可以运行一个中立示例，无需看到 FreeInference 主机清单，也不需要
   FreeInference 账号。
7. 公开仓库不含任何 secret value，也不存在可以部署到 FreeInference 基础设施的
   workflow。
8. identity/inference-grant 契约、sandbox egress 边界和 job fencing 语义保持不变。

### 非目标

- 重写 scheduler、host protocol、sandbox lifecycle 或 gateway contract。
- 把 Cloud Agent 搬回 gateway 或搬入 FreeInference 仓库。
- 在本次切换中重命名数据库、表、OAuth client ID、JWT audience、Compose project、
  volume、network 或 host directory。
- 支持任意可执行的 distribution plugin。
- 把所有 Web base path 都变成运行时选项；Next.js 会在结构上编译 base path，
  因此 v1 只发布 §6 定义的两个受支持中立变体。
- 解决跨 host retained-sandbox 迁移或持久冷存储问题。
- 在回滚中自动降级数据库 migration。
- 在本设计实施过程中直接公开仓库或 GHCR package。可见性变更仍是一个明确、独立的
  最终操作。

## 3. 决策记录

### D1 — `hybridInference-cloud-agent` 是可复用上游

它拥有应用代码、数据库 migration、公开服务契约、通用 Dockerfile、参考部署示例、
CI 和制品发布。切换完成后，它不再拥有任何真实部署。

仓库可以说明运营方如何提供配置，但不得包含真实运营方的 domain、machine label、
managed path、private network topology、app registration、monitoring destination 或
deployment credential。

### D2 — `freeInference` 拥有 FreeInference Cloud Agent distribution

现有私有仓库已经拥有 FreeInference gateway distribution 及其 `upstream.lock`。
Cloud Agent 应作为第二个上游加入，而不是再创建第三个产品仓库。

它拥有以下权限和责任：

- 产品品牌和公开 URL；
- feature 与 runtime policy；
- GitHub/GitLab application coordinate；
- Compose topology 与 host path；
- deployment workflow、runner label、health check、backup 和 rollback；
- 一份原子固定全部 Cloud Agent 制品的 `cloud-agent.lock`。

Secret **值**仍保存在 GitHub Environment 或未跟踪的 host environment file 中。
私有仓库是所有权边界，不是 secret store。

### D3 — 上游 merge 只发布，绝不部署

切换完成后，本仓库不再有任何指向 FreeInference staging 或 production 的
`workflow_run` 或 dispatch 路径。只有 `dev` 上成功的 push run 或签名 release
可以发布候选制品。

公开 pull request：

- 只在 GitHub-hosted 或等效的一次性隔离 runner 上运行；
- 不获得 environment secret；
- 永不发布可信 tag；
- 永不接触 deployment network。

只有在 self-hosted CI runner 可抛弃，或既无部署凭据、也无部署网络访问能力，且
永不执行 fork 代码时，才允许使用它。FreeInference deploy runner 永远不能作为
上游 CI runner。

### D4 — 一份原子 lock 是部署坐标

`freeInference/cloud-agent.lock` 记录：

- 上游仓库和一个 40 位十六进制 source commit；
- 可选 release tag；
- distribution/config interface version；
- 每个必需 runtime image 的不可变 digest。

同一份 lock 中的所有 image 必须携带相同的
`org.opencontainers.image.revision`。lock 不能只更新 web image 或只更新 runner；
局部 fleet 会把 control-plane/host protocol 变成未经评审的滚动兼容实验。

FreeInference distribution commit 是第二个版本坐标。因此运行系统有两个明确 revision：

```text
upstream source revision      image 中包含什么代码
distribution revision         哪套配置与部署策略选择了这些代码
```

### D5 — 上游为六个运行角色发布七个中立制品

制品集合如下：

1. `control-plane`——从现有共享 Python image 提供 API、MCP relay、migration、
   publisher 和 reaper 角色；
2. `web-root`——以空 base path 编译的 Next.js Web 服务；
3. `web-agents`——同一份源码以 `/agents` 编译的 Web 服务；
4. `host`——runner、workspace broker 和 supervisor 共用的 Python image；
5. `sandbox`——固定 Agent CLI runtime 的 image；
6. `egress-proxy` 和 `tunnel`——两个小型基础设施 image。

egress proxy 与 tunnel 虽组成一个基础设施角色，但它们是两个独立制品。因此上游
release set 有七个 digest。一个 distribution 只选择一个 Web 变体：FreeInference
lock 固定包括 `web-agents` 在内的六个 digest；公开示例则选择 `web-root`。

每个 package 对一个 source SHA 只构建一次，携带 OCI source/revision label，并且
只按 digest 部署。tag 只是发现入口，绝不是部署坐标。v1 只发布已经被 CI 证明的
platform set；新增 architecture 只有在相应 image 和 live conformance gate 都存在后，
才能写入 manifest。

### D6 — 产品身份是运行时状态；`basePath` 是构建变体

目前大部分 Web 身份通过 `NEXT_PUBLIC_*` 在编译期写入。这会迫使每个 distribution
和 environment 重建 Web image，直接破坏 D4。

改造后，Web server 在运行时读取一个有边界、非 secret 的 `AGENT_WEB_*` allowlist，
并在 server rendering 时把 `RuntimeSiteConfig` 传入现有 `SiteConfigProvider`。
因此初始 HTML 和 metadata 会直接带上正确产品身份，不出现客户端闪烁，也无需调用
含 gateway 私有信息的 endpoint。

server layout 使用 runtime rendering（例如 `generateMetadata` 加 no-store/dynamic
边界），避免 Next.js 在 `next build` 时冻结 host environment。同一个 image digest
以两套允许的 runtime value 启动时，必须能渲染两套不同的 title/config object。

运行时字段包括：

- application 与 organization display name；
- organization、status 和 gateway link；
- 可公开的 feature flag；
- environment 与已部署 source revision。

浏览器可见 API path 尽量从结构变体和 same-origin routing 推导。任何 secret 都不得
进入这个对象。

Next.js `basePath` 仍然是 build-time 输入。v1 只支持 root 和 `/agents` 两种变体；
任意其他 prefix 不会被静默接受。两个变体来自同一个 commit，且都是中立制品。

### D7 — Distribution manifest 负责渲染配置；应用代码不读取它

FreeInference 新增 `distributions/freeinference/cloud-agent/distribution.yaml`。
validator 将经过评审的值转换为 Compose/runtime environment；Cloud Agent 继续读取
现有 environment interface。

由此拒绝两种 split truth：

- Cloud Agent 内不再增加与现有 Pydantic/environment setting 并存的
  `DISTRIBUTION_CONFIG_PATH` 查找；
- 不从 distribution checkout import Python hook。

manifest 的 `schema_version` 与 `cloud-agent.lock` 中的 interface version 配对。
FreeInference deploy 遇到无法理解的新 manifest/image 组合时必须拒绝运行。

### D8 — 兼容标识符与品牌分别迁移

以下内容不在切换时批量重命名：

- `cloud-agent` OAuth client/audience identifier；
- 现有环境变量名；
- 数据库、表、列名；
- Compose project、network、volume、host state root 和 enrolled-host identity；
- 原生 Agent CLI session 中持久化的 runtime provider ID；
- 已部署 host code 读取的 image label。

对于带产品名的 harness 集成，上游先新增中立 canonical name，同时保留兼容 alias：

- 新 wrapper 和 provider default 使用 `hybridinference`；
- 在 retained session 仍依赖旧 ID 的兼容窗口内，FreeInference 显式选择 provider ID
  `freeinference`；
- 只要仍存在旧 retained container，driver 就先检测中立 binary，再回退到 legacy binary；
- image builder 写入新的 `org.hybridinference.cloud-agent.*` label，reader 在一个
  兼容 release 内回退读取 `org.freeinference.*`。

只有 inventory 证明没有 live retained container 或 native session 依赖旧标识符时，
才能移除兼容逻辑。公开仓库中保留有文档和测试的兼容 alias 是可以接受的；为了删掉
一个词而悄悄破坏 live session 则不可接受。

### D9 — Production Compose 与 CD 只存在于 FreeInference

上游 Dockerfile 继续作为构建接口；上游 Compose file 只作为 reference/example topology。

FreeInference 拥有 production Compose 和 deployment script。它们必须：

- 引用由 lock 解析的 image，不含 `build:` directive；
- 第一次切换时保留现有 Compose project、network、volume 和 host-state name；
- 校验 image package、digest、revision 和 host architecture；
- 在替换 API 前执行 migration；
- 按现有 drain/grace 语义替换 runner；
- 执行 health gate 和真实 contract smoke；
- gate 失败时恢复上一版 distribution revision 与 image set。

### D10 — 不允许两个 control plane 并行连接 live database

control plane 会运行 publisher、reaper、scheduler 和 token/state transition。即使没有
HTTP 请求，一个指向同一数据库的“shadow” control plane 也不是只读对比；除非另行
设计 leader fence，否则禁止这样做。

候选制品先在隔离数据库中演练。staging 切换随后使用现有数据库和 identity，执行一次
有 health gate 的原地替换。

### D11 — 数据库 migration 留在上游；backup 与调用归 distribution

Migration source 应与读取该 schema 的代码共同维护。私有 deploy 决定何时运行、执行
所需 backup，并记录结果。

安全时可自动回滚 image，但 schema downgrade 不能假装自动：production 晋级前，
每次 lock bump 都必须把 migration 分类为 backward-compatible，或提供明确的
rollback procedure。没有这一答案的候选版本不能晋级。

### D12 — Gateway contract 保持公开且与 distribution 无关

Identity、grant 和 internal lookup schema 留在 `contracts/`，使用中立 title 和
example server。FreeInference 拥有 live endpoint smoke test 与具体 URL。

两个仓库都不共享签名 secret。distribution 只提供 HTTP 契约已经要求的 Cloud
Agent 侧 credential 和公开 gateway coordinate。

### D13 — Source history 是公开输入，不是部署制品

部署主机不再 reset 本仓库 checkout 或从源码构建。它们只管理 FreeInference checkout
并拉取 lock 固定的 image。

旧 `/srv/freeinference-cloud-agent` checkout 在定义好的 rollback window 内保持不动，
之后再通过独立、明确批准的操作归档或移除。切换脚本不得执行 `git clean`，不得删除
volume 或 Docker data root。

### D14 — 晋级流程保持非对称

- 绿色的 upstream `dev` 候选版本最多触发一个 **lock PR**，不能触发部署。
- FreeInference `dev` 上绿色的 lock/config merge 部署 staging。
- production 要求相应 distribution revision 已进入 `main`，并由人工触发部署。
- staging 与 production 选择同一组 upstream image digest；两者的非 secret runtime
  config 与 secret 不同。

前两个成功的 Cloud Agent 晋级周期内，lock PR 自动合并保持关闭。之后可以沿用现有
FreeInference bump bot 的 fail-closed verdict 模型。

## 4. 仓库所有权

### 4.1 公开上游

最终上游结构如下：

```text
hybridInference-cloud-agent/
  cloud_agent/                  application and API
  cloud_agent_host/             host, supervisor, drivers, broker
  contracts/                    gateway-facing HTTP schemas
  migrations/                   agent database migrations
  web/                          neutral web application
  deploy/
    images/ or current Dockerfile locations
    example/                    runnable reference Compose, placeholder values
  docs/                         developer, operator-interface, security docs
  .github/workflows/
    ci.yml
    publish.yml                 artifacts only; no environment deployment
```

它拥有稳定接口：

- 环境变量名及其校验；
- control-plane/host protocol 与 schema version；
- 消费方依赖的 image role、port、health endpoint、label 和 volume；
- 受支持 Web variant；
- 使用占位 domain 和一次性 local state 的 runnable example。

### 4.2 私有 FreeInference distribution

包括现有 console bridge 在内，目标私有仓库结构如下：

```text
freeInference/
  cloud-agent.lock
  distributions/freeinference/cloud-agent/
    distribution.yaml
    branding.yaml
    runtime-policy.yaml
    deploy/
      common.env
      staging.env
      production.env
    smoke/
      control_plane.py
      sandbox_job.py
  deploy/cloud-agent/
    docker-compose.control-plane.yml
    docker-compose.host.yml
    deploy-control-plane.sh
    deploy-host.sh
  deploy/docker-compose.console-agent-network.yml  existing console bridge
  ops/
    validate_cloud_agent_lock.py
    write_cloud_agent_lock.py
    read_cloud_agent_image_revision.py
  .github/workflows/
    bump-cloud-agent.yml
    deploy-cloud-agent-staging.yml
    deploy-cloud-agent-production.yml
```

提交进仓库的 `.env` 只包含非 secret 部署选择。secret value 由 GitHub Environment
或现有未跟踪 host file 提供，并且只按变量名引用。

现有 `deploy/docker-compose.console-agent-network.yml` 继续作为 FreeInference 拥有的
集成契约，不迁入上游。它把 gateway console frontend 同时接入常规 network 与外部
`cloud-agent` network，使 `/agents` proxy 可以访问私有 Cloud Agent Web 服务。

### 4.3 Host/GitHub 拥有的状态

两个 Git 仓库都不拥有：

- 数据库密码或 DSN；
- session、control、dispatcher、enrollment、broker、MCP 或 grant-dispatch secret；
- GitHub App/GitLab OAuth 私有 credential；
- SSH private key 或私有 `known_hosts` inventory；
- live `.env`、数据库内容、Docker data root、workspace 或 retained sandbox state。

distribution 记录每个必需 key 及其 provisioning 位置，但绝不记录值。

## 5. Lock 与制品契约

### 5.1 `cloud-agent.lock`

这份 lock 有意与 gateway 现有的 `upstream.lock` 分开。两个上游具有不同的 release
节奏、制品集合、回滚风险和 staging gate；把它们放进同一个文件，会让仅更新 Agent
的 bump 看起来同时改变了 gateway 部署坐标。

v1 规范结构如下：

```yaml
schema_version: 1

upstream:
  repository: HarvardMadSys/hybridInference-cloud-agent
  release: null
  source_commit: 0123456789abcdef0123456789abcdef01234567

interfaces:
  distribution: 1
  control_plane_host: 1
  sandbox_runtime: 1
  gateway_contracts: 1

images:
  control_plane: ghcr.io/harvardmadsys/hybridinference-cloud-agent-control-plane@sha256:<64-hex>
  web:
    variant: agents
    image: ghcr.io/harvardmadsys/hybridinference-cloud-agent-web-agents@sha256:<64-hex>
  host: ghcr.io/harvardmadsys/hybridinference-cloud-agent-host@sha256:<64-hex>
  sandbox: ghcr.io/harvardmadsys/hybridinference-cloud-agent-sandbox@sha256:<64-hex>
  egress_proxy: ghcr.io/harvardmadsys/hybridinference-cloud-agent-egress-proxy@sha256:<64-hex>
  tunnel: ghcr.io/harvardmadsys/hybridinference-cloud-agent-tunnel@sha256:<64-hex>
```

上面的 literal package name 是提案，只有创建第一批 package 时才最终固定；schema
property 才是契约。

validator 拒绝以下情况：

- 出现未知 key 或 schema version；
- repository 不是精确指定的上游；
- source commit 不是 40 位十六进制值；
- 使用 tag、mutable reference、缺失 digest 或重复 role；
- package 不在精确 role allowlist 中；
- 缺少 host architecture；
- image 的 source/revision label 与 `source_commit` 不同；
- 各 image role 来自不同 source commit；
- interface version 不在当前 distribution 支持范围内；
- FreeInference 选择了 `agents` 以外的 Web variant。

### 5.2 制品发布

pull request 的 CI 对所有受影响 image 执行 `push: false` 构建。push 到 `dev` 时，
制品发布要等同一套完整 CI Gate 通过，然后：

1. 从 checkout 的完整 SHA 构建每个支持 platform；
2. 写入：
   - `org.opencontainers.image.source`；
   - `org.opencontainers.image.revision`；
   - `org.opencontainers.image.version`；
   - 声明的 Cloud Agent interface version；
3. push 一个完整 SHA 的发现 tag，例如 `dev-<40-hex>`；
4. 对支持多个 platform 的角色组装 multi-platform index；
5. 解析并输出不可变 digest；
6. 在可用时生成 SBOM 和 GitHub artifact attestation。

bump bot 既不只信任 tag，也不只信任 workflow success。它解析 tag，遍历
manifest/config，校验 revision 和 package name，最后写入 digest。重新运行 workflow
可能让 tag 指向新字节，但已经提交的 lock 永远不会随之移动。

release tag 复用相同 image，只增加一个 release discovery tag；不会对已经按该
release commit 发布的源码重新构建。

### 5.3 支持的平台契约

目标 v1 发布以下 platform：

| 角色 | v1 必需 platform |
|---|---|
| control plane | `linux/amd64`、`linux/arm64` |
| web root / web agents | `linux/amd64`、`linux/arm64` |
| host | `linux/amd64` |
| sandbox | `linux/amd64` |
| egress proxy | `linux/amd64` |
| tunnel | `linux/amd64` |

U3 补齐当前 matrix 尚未覆盖的构建。小型基础设施 image 也许在其他架构上技术上可以
build，但“支持”意味着完整 runtime 和 smoke suite 已经在那里通过。distribution
不能仅凭 image manifest 推断支持状态。

### 5.4 Bump 决策

`freeInference/.github/workflows/bump-cloud-agent.yml` 复用现有 gateway bump policy：

```text
upstream dev HEAD 与 cloud-agent.lock 不同
  -> 该精确 SHA 有 completed successful push CI run
  -> 所有必需 full-SHA artifact tag 都存在
  -> 每个 digest 与 revision label 校验通过
  -> distribution/interface compatibility 校验通过
  -> 创建一个仅修改 lock 的 PR
  -> 所有 FreeInference check 必须通过
  -> 初始 rollout 期间由人工合并
```

缺少 CI 或 artifact 属于“尚未就绪”；bot 安静退出，之后重试。package、revision、
schema 或 provenance 不匹配则是必须标红的硬失败。

读取上游并创建 PR 的 installation token 只授权这两个仓库，不含部署权限。registry
读取使用 GHCR 接受的最低权限 token；deployment job 不使用这两个 token 中的任何一个。

## 6. Distribution 配置契约

### 6.1 Manifest

建议的初始 FreeInference manifest：

```yaml
schema_version: 1

distribution:
  id: freeinference
  display_name: FreeInference Cloud Agent

gateway:
  public_base_url: https://freeinference.org
  authorize_url: https://freeinference.org/authorize

web:
  variant: agents
  base_path: /agents
  public_base_url: https://freeinference.org/agents
  api_base_url: /agents/api
  organization_name: FreeInference
  organization_url: https://freeinference.org
  status_url: ""

features:
  rag_link: true

runtime:
  model_provider_id: freeinference
  default_execution_profile: untrusted-kata-v1

compatibility:
  accept_legacy_gateway_env: true
  read_legacy_runtime_labels: true
```

上述具体 production value 按设计只存在于私有仓库。该文件不包含 token、key、
password、DSN、private hostname、runner label 或 filesystem path。

host topology 有意不放在这份产品 manifest 中，而是归私有 Compose/env layer 所有；
因为两个 environment 可以共享同一套产品身份，却使用不同 machine、port、state root
和 runner capacity。

restartable/shared-live preview 等 environment rollout switch 也归 staging/production
env layer，而不归 product manifest。两个 environment 可以有意在不同时间推进。

### 6.2 渲染到现有应用接口

FreeInference validator/renderer 把 manifest value 映射为现有 environment interface。
代表性映射如下：

| Manifest 值 | Runtime 接口 |
|---|---|
| `gateway.public_base_url` | `GATEWAY_BASE_URL`、`AGENT_WEB_GATEWAY_URL` |
| `gateway.authorize_url` | `GATEWAY_AUTHORIZE_URL` |
| `web.public_base_url` | `AGENT_WEB_BASE_URL` |
| 从 public URL 推导并校验的 callback | `AGENT_SESSION_REDIRECT_URI` |
| `web.*` identity field | server-runtime `AGENT_WEB_*` |
| `runtime.model_provider_id` | `AGENT_MODEL_PROVIDER_ID` |
| `runtime.default_execution_profile` | `AGENT_DEFAULT_EXECUTION_PROFILE` |

renderer 在部署前校验跨服务等式：

- callback 位于选定 public base URL 下；
- callback 与 gateway distribution 允许的 redirect 完全一致；
- 除非经过明确评审，gateway base 与 authorize origin 必须一致；
- 所选 Web variant 编译进 image 的 base path 必须等于 manifest；
- console `/agents` proxy target 与命名 Docker network 必须和 Cloud Agent Compose 一致；
- staging value 不得引用 production secret、database、project 或 state root，反之亦然。

生成的 environment output 只含非 secret 值。Compose 使用 required-value expression
按名字引用 secret variable，并在执行时从 host environment 解析。

### 6.3 运行时 Web 身份

只有以下字段不再是 `NEXT_PUBLIC_*` build argument，Web image 才真正中立：

- app name；
- organization name 和 URL；
- status 与 gateway URL；
- feature flag；
- build/deployment display revision。

`web/src/app/layout.tsx` 及 `SiteConfigProvider` 上层 server boundary 从 Web container
environment 读取严格 allowlist，只把该 public object 序列化到页面。client code 永远
不会获得完整 environment。

image 只保留结构性 build input：

- `web-root` 使用 `NEXT_PUBLIC_BASE_PATH=`；
- `web-agents` 使用 `NEXT_PUBLIC_BASE_PATH=/agents`。

API path 从所选 topology 推导，或在运行时以经过校验的 same-origin relative path
提供。cross-origin cookie deployment 只有在明确配置且另有测试时才允许。

### 6.4 Harness provider identity

展示给用户的文字与 Agent CLI 持久化的 ID 是不同字段：

```text
display name       FreeInference       distribution-owned presentation
provider ID        freeinference       compatibility-selected machine ID
gateway URL        agent-gateway       deployment-owned relay coordinate
grant token        per attempt         control-plane-owned secret capability
```

中立 wrapper 从 runner 生成且经过评审的配置中接收 provider ID/display name。不可信
job 不能选择其他 gateway URL，也不能注入 long-lived credential。

兼容窗口内 FreeInference 保留 provider ID `freeinference`，让 native-resume state
继续可读。全新的第三方部署使用 `hybridinference`。U2 完成后，上游不再硬性要求这
两个 ID 相同。

## 7. FreeInference 部署模型

### 7.1 Distribution 拥有 Compose

私有 Compose file 先从当前已验证 topology 复制一次，然后把源码构建改为 locked image：

```yaml
services:
  control-plane:
    image: ${CLOUD_AGENT_CONTROL_PLANE_IMAGE:?resolved from cloud-agent.lock}
  web:
    image: ${CLOUD_AGENT_WEB_IMAGE:?resolved from cloud-agent.lock}
  runner:
    image: ${CLOUD_AGENT_HOST_IMAGE:?resolved from cloud-agent.lock}
  workspace-broker:
    image: ${CLOUD_AGENT_HOST_IMAGE:?resolved from cloud-agent.lock}
```

sandbox image、egress proxy 和 tunnel digest 从同一份 lock 解析，并传给 host topology。
任何 production Compose service 都不允许有 `build:` key。部署工具校验后将 locked
digest reference 渲染到临时 environment file；运营者不能通过普通 host `.env`
偷偷替换成 tag。

第一次切换保留：

- control-plane 与 sandbox Compose project name；
- gateway console proxy 消费的命名 `cloud-agent` network；
- Postgres volume ownership；
- host credential、workdir、snapshot 与 Docker data root；
- egress network 与 tunnel alias；
- 发布在 loopback 上的 port；
- runner replica count 与 graceful stop behavior。

之后可以通过显式 state migration 清理这些名字；本阶段不把它们当作品牌。

现有 console overlay `deploy/docker-compose.console-agent-network.yml` 继续存在于
FreeInference deployment。它声明 gateway frontend 同时加入 `hybridinference` 和外部
`cloud-agent` network。Cloud Agent Compose 负责创建后者；console overlay 只消费它。
当 `EXPECT_CLOUD_AGENT=true` 且 `docker network inspect cloud-agent` 失败时，现有
gateway-console deploy 和 restore path 必须在替换 console 前 fail closed。替换后，
`/agents` 属于 console health gate。Cloud Agent workflow 也要在替换 Web service 前
单独校验该 network，并在替换后探测同一路由。这样可以避免任一 stack 单独健康，
但组合后悄悄发布一条损坏的 Agent route。

### 7.2 Staging 部署

FreeInference `dev` 上影响 `cloud-agent.lock`、对应 manifest 或 Cloud Agent deploy
file 的 merge，只触发一个 staging workflow：

1. 校验 FreeInference distribution revision；
2. 解析并校验 `cloud-agent.lock`；
3. 解析 host architecture 并校验每个选中 digest；
4. 将目标 interface version 与 manifest 对比；
5. 记录当前 distribution revision、lock、运行中的 image ID 和 health status；
6. 在修改任何运行中 container 前，先 pull 全部 candidate image；
7. 执行隔离的 migration/config preflight；
8. 部署 control plane 与 Web，然后要求 health、identity/JWKS、session 和
   gateway-grant smoke check 通过；
9. 通过现有 console overlay 校验外部 `cloud-agent` network 与 gateway console
   `/agents` route；
10. drain/替换 host service，但不删除 retained sandbox 或 state root；
11. 通过 production-shaped relay topology 运行一个真实 no-publish sandbox job；
12. 记录成功的 upstream 与 distribution revision。

不在受监控 distribution/deploy path 内的纯文档变更不会部署。

### 7.3 Production 晋级

production deploy 保持手动：

1. staging 必须报告精确 candidate lock 和成功 smoke result；
2. 选中的 FreeInference commit 晋级到 `main`；
3. production workflow 校验 `main` 确实包含完全相同的 lock；
4. 运营者执行按 migration 分类要求的数据库备份；
5. 使用 production runtime config 部署同一组 digest；
6. production health 和一次有界 smoke request 通过后才记录成功。

任何 production workflow 都不接受自由填写的 upstream SHA 或 image 参数。rollback
选择之前已经提交的 FreeInference revision/lock pair。

### 7.4 Deployment record

distribution 按 environment 记录：

```yaml
distribution_revision: <freeInference SHA>
cloud_agent_source_revision: <upstream SHA>
cloud_agent_lock_digest: <sha256 of normalized cloud-agent.lock>
images:
  control_plane: <running digest>
  web: <running digest>
  host: <running digest>
  sandbox: <selected digest>
  egress_proxy: <running digest>
  tunnel: <running digest>
deployed_at: <UTC timestamp>
```

这取代当前含义模糊的做法：用同一个 upstream `.deployed-sha` 同时表示 code revision
和 deployment policy revision。

upstream revision/interface value 写在不可变 image label 上；distribution revision
和 lock digest 写在 Compose container label 上。host 把两个坐标都传入每个动态创建
sandbox 的 label，使 retained container 在 fleet 前进后仍然可追溯。

## 8. 失败与回滚行为

### 替换前

任何 package、digest、platform、revision label、config field、secret、network、backup
或 gateway preflight 缺失，都必须在修改 container 前失败。已经运行的服务保持不动。

### Control-plane 失败

如果 migration 或 control-plane/Web health 失败：

- 恢复之前的 Compose input 和 image digest；
- 重新创建上一版 control-plane/Web set；
- 重新执行其 health check；
- failed deployment record 与 last-known-good 分开保留。

workflow 不声称能够回滚 schema。被分类为 non-backward-compatible 的 migration 必须
在晋级前配备运营者编写的 downgrade/restore plan。

### Host 失败

如果 host replacement 或真实 sandbox smoke 失败：

- 停止 candidate runner 接取新任务；
- 恢复之前的 host/infrastructure image set；
- 保留 host credential 和全部 Docker/state root；
- 让现有 attempt lease 按正常 fencing 逻辑终止失去的任务；
- 任何无法再访问的 retained sandbox 都必须报告为切换失败。

任何自动 branch 都不得运行 `docker compose down -v`、`docker system prune`、
`git clean`，也不得删除 managed path。

### Distribution workflow 失败

bump 失败时 lock 不变；staging 失败时 production 不变；production 失败时，在 schema
compatibility 允许的情况下恢复上一份 lock 的 image set，并携带两个版本坐标通知运营者。

## 9. 当前文件到目标所有权的映射

| 当前 Cloud Agent path/surface | 最终 owner 与处理方式 |
|---|---|
| `cloud_agent/**`、`cloud_agent_host/**`、`migrations/**` | 上游；中立应用与 schema code |
| `contracts/*.openapi.yaml` | 上游；使用中立 title/example server。FreeInference 保留 live agreement/smoke test |
| `deploy/**/Dockerfile*` | 上游；可复现制品构建接口 |
| `deploy/control-plane/docker-compose.yml` | 上游只保留 reference example；FreeInference 复制并拥有 production Compose |
| `deploy/host/docker-compose.sandbox.yml` | 上游只保留 reference topology；FreeInference 拥有真实 host topology |
| `deploy/control-plane/deploy.sh`、`deploy/host/deploy.sh` | 通用 helper 可为示例保留；reset 源码的 production 行为迁移为 FreeInference 中的 digest deployment |
| `.github/workflows/deploy-staging.yml` | handoff 后从上游移除；由 FreeInference 基于 lock 的 staging CD 取代 |
| `.github/workflows/deploy-prod.yml` | production handoff 后从上游移除；由 FreeInference 手动 CD 取代 |
| FreeInference self-hosted runner label | 只存在于 FreeInference workflow |
| `web/src/config/env.ts` 产品身份 | 上游 runtime server config；中立 default |
| `Dockerfile.web` 产品 build argument | 只保留结构性 base-path build input |
| `pi-freeinference`、`opencode-freeinference`、`kilo-freeinference` | 上游新增中立 canonical wrapper，并保留有时限的 compatibility alias |
| `freeinference/<model>` provider ID | 由 distribution 为兼容显式选择；不做全局替换 |
| `org.freeinference.runtime.*` label | 兼容期双写/双读；live inventory 清零后迁移到中立 namespace |
| `FREEINFERENCE_BASE_URL` | 带 warning 的 deprecated input alias；canonical interface 仍为 `AGENT_GATEWAY_URL` |
| FreeInference Compose project、path、image tag、network 和 state root | 私有 distribution；第一次切换时原样保留 |
| `freeInference/deploy/docker-compose.console-agent-network.yml` | 保持私有；维持 gateway frontend 的外部 `cloud-agent` network attachment 与 `/agents` 可用性契约 |
| `README.md`、`AGENTS.md`、`ENVIRONMENT.md` | 当前文档改为上游中立 |
| `MIGRATION.md`、`docs/cutover.md`、历史 design record | 可以保留事实准确的历史 FreeInference 引用，但要明确标注为历史内容并清除私有值 |
| live domain、OAuth/GitHub App coordinate、alert destination、backup policy | 私有 distribution 或 environment |

因此，对整个上游仓库执行“不得出现 FreeInference 字符串”的测试并不正确。可执行的
policy 应是：

1. 显式 compatibility/historical allowlist 之外，不得出现当前 FreeInference
   deployment coordinate；
2. executable code 中不得存在 FreeInference product default；
3. 每个 allowlist 中的 compatibility identifier 都必须有 owner、removal condition
   和 regression test。

## 10. 切换计划

handoff 有意拆成多个阶段。替代路径成功部署并完成一次回滚演练前，不得删除上一条
部署路径。

### Phase 0 — 冻结与盘点

在合并 runtime/publication 变更前：

1. 记录 staging 与 production 已部署的 control-plane 和 host source SHA。
2. 记录运行中 container image ID、Compose project、service name、network、volume、
   published port、Docker data root 和全部 persistent state path。
3. 仅记录 environment 及 GitHub Environment/Repository variable 的**变量名清单**；
   secret value 绝不导出到 artifact。
4. 核验 control-plane database backup 与 restore instruction。
5. 统计 retained/shared sandbox，并记录其中使用了哪些 legacy wrapper/provider ID
   和 image label。
6. 运行当前 gateway preflight 和一次 no-publish agent smoke。
7. 保存现有源码部署 workflow run URL，作为 rollback baseline。

Phase 0–4 期间，不批量合并开源准备 PR。每次只合并一个，并观察一次 staging 结果。

### Phase 1 — 增加 legacy deployment kill switch

在新 owner 就绪前，当前 staging job 先增加一个 repository variable gate：

```text
LEGACY_DIRECT_STAGING_DEPLOY=true   current behavior
LEGACY_DIRECT_STAGING_DEPLOY=false  jobs skip; no host is touched
```

guard 落地前，先把变量设为 `true`，这样 guard commit 自身的绿色 CI 仍会完整演练当前
pipeline。control-plane 和 host job 都必须检查该 gate，并由 workflow-policy test 覆盖。

旧 production workflow 同样增加 fail-closed 的
`LEGACY_DIRECT_PRODUCTION_DEPLOY` gate。它保持 `true`，直到私有 production workflow
完成 dry run；handoff 前再设为 false。

这个 switch 是迁移 fuse，不是最终架构。切换后两个 workflow 都从上游删除。

### Phase 2 — 让上游制品中立且可发布

在 legacy staging gate 仍为 true 时落地：

1. runtime Web identity 和两个结构性 Web variant；
2. 中立 wrapper/label 支持及 compatibility read；
3. OCI label 与 interface-version constant；
4. pull request 上完整 image CI；
5. `dev` push 全绿后按 digest 发布；
6. policy test 证明 pull request 无法触达任何 publish 或 deployment path。

该 merge 会执行最后一次由源码上游拥有的 staging deployment。这是有价值的：在部署
所有权改变前，staging 证明同一份代码仍可工作。Phase 0 必须先证明当前 environment
显式提供 FreeInference branding；如果没有，必须在中立 default 落地前补上该值。

### Phase 3 — 合入处于禁用状态的 FreeInference consumer

在 `freeInference` 中新增：

- 固定到 Phase 2 candidate 的 `cloud-agent.lock`；
- manifest、validator、renderer 和测试；
- 只使用 digest 的 control-plane 与 host Compose；
- 关闭 auto-merge 的 bump workflow；
- 初始只允许 manual dry-run 的 staging/production workflow；
- deployment-record 与 rollback tooling。

CI 为两个 environment 渲染 `docker compose config`，确认没有 `build:`，检查全部跨服务
coordinate，并对 lock validator 做 mutation test。该阶段合并时不执行任何部署。

### Phase 4 — Staging 所有权交接

1. 使用私有 workflow 只校验/pull lock，不替换服务。
2. 在隔离数据库和本地 contract service 上运行所有 image。
3. 使用现有 project、network、volume 和 state name，执行私有原地 staging deploy。
4. 校验 login、JWKS、grant mint/revoke、MCP、source-control connection、host
   registration、job claim、一次 no-publish turn、terminal/files/Git，以及 retained
   chat resume。
5. 回滚一次到之前的源码部署，重复 smoke，再次部署 locked candidate。没有排练过的
   rollback path 不能作为 cutover path。
6. 设置 `LEGACY_DIRECT_STAGING_DEPLOY=false`。
7. 触发一次无害 upstream CI push，证明没有创建 staging deployment。
8. 对相关 FreeInference path 启用自动 staging deployment。

staging database 上绝不同时运行第二个 live control plane。

### Phase 5 — Production 所有权交接

1. 保持 staging 已经演练过的精确 upstream digest set。
2. 将 FreeInference distribution revision 晋级到 `main`。
3. 以 validate-only 模式运行私有 production workflow。
4. 按分类要求完成 database backup。
5. 设置 `LEGACY_DIRECT_PRODUCTION_DEPLOY=false`。
6. 从 FreeInference 部署，并完成 production smoke gate。
7. 在 rollback window 内，让旧 upstream workflow 保持禁用，同时保留旧 managed
   checkout 不动。
8. 只有当私有 deploy 无法恢复服务，且运营者显式选择该 rollback 时，才重新启用旧
   workflow。

完成两次成功的私有 production deployment 并度过约定 rollback window 后，才在
上游删除旧 workflow，并通过独立运营变更退役 source checkout。

### Phase 6 — 开源清理

只有 staging 和 production 都已经由 distribution 拥有后，才执行：

- merge/rebase 中立命名与仓库引用变更；
- 替换公开文档和示例中的当前产品 coordinate；
- 只保留有文档说明的 compatibility/historical reference；
- 对完整保留历史运行 gitleaks；
- 校验 GitHub App/OAuth callback 与 repository access；
- 配置 branch protection、Actions permission、GHCR access、security reporting 和
  公开 issue/PR template；
- 将仓库与 package visibility 作为两个独立、可审计操作进行切换。

公开仓库不是一次 deployment，绝不能把两者绑定成同一个操作。

## 11. 当前开源准备 PR 的处理方式

四个现有 PR 是本设计的输入，但它们各自都不能单独构成 distribution 边界。

| PR | 处理方式 |
|---|---|
| #98 neutral defaults | FreeInference runtime identity 显式化后再 rebase。API title 和 display default 可以中立化；compatibility ID 仍按 D8 处理 |
| #99 public-safe CI | 在允许任何外部贡献者提交 PR 前落地。fork runner guard 是必要的 defense-in-depth；但 direct deployment 仍必须按 D3 移出 |
| #100 SECURITY/CONTRIBUTING | 配置好私有 vulnerability reporting 与最终 contribution flow 后重新检查；文案必须描述真实 channel |
| #101 repository rename | distribution/compatibility 工作完成后再 rebase。可以更改 repository guard 和 package metadata；persisted provider ID、label、path 与 project 不随之重命名 |

Phase 1 fuse 存在前，上述任何 PR 合入 `dev` 并通过 CI 后，都会触发当前 staging
deployment，纯文档 PR 也一样。production 仍保持手动。

本 design-document PR 在 branch 上不改变任何 runtime；但如果在 Phase 1 前合入，
当前 workflow 同样会执行一次 no-code staging deployment，必须像其他 merge 一样观察。

## 12. 安全不变量

只有以下各项全部由机器强制执行，拆分才算完成：

1. upstream pull-request job 不使用 FreeInference self-hosted runner 或 deployment
   environment。
2. upstream workflow 不含 FreeInference deployment token、runner label、managed
   host path 或 production/staging dispatch。
3. upstream publication 只在 trusted push/release 上，使用仅有 package-write 权限的
   repository token。
4. distribution deploy 只接受 committed lock 中的 digest reference 和精确 allowlist
   package。
5. tag 不能覆盖 committed digest。
6. 每个运行中 image 的 revision label 都与 lock 中唯一 source commit 相同。
7. source、generated config artifact、workflow log、deployment record 和 image layer
   中都不存在 secret value。
8. sandbox 仍只获得每次 attempt 独立的 short-lived grant，并且在当前 egress phase
   只能访问经过评审的 gateway/MCP relay destination。
9. distribution 不能通过修改 presentation field 扩大 sandbox egress。
10. gateway signing 与 API-key secret 永不进入任一 Cloud Agent repository、image
    或 host。
11. host state root 与 retained Docker data 绝不突破现有 sandbox 边界挂载进不可信 job。
12. config、secret、platform、image、contract 或 health verdict 任何一项缺失，
    都必须在晋级前 fail closed。

公开历史扫描要覆盖所有计划公开的 ref，而不只是 working tree。如果某个旧 retained
commit 仍包含 credential，那么即使当前版本已经删除，该 credential 仍然会随历史公开。

## 13. 验证与验收门禁

### 13.1 上游门禁

- runtime site-config allowlist 与 server rendering 的 unit test。
- two-container test：证明同一个 Web digest 不经 rebuild，可以渲染两套 runtime identity。
- root 与 `/agents` 两种 variant 的 Web build 和 test。
- contract test：证明两种 variant 下 browser API/sign-in path 都正确。
- wrapper test：覆盖中立 default、FreeInference compatibility selection、缺少变量时拒绝，
  以及 credential 不泄露。
- host test：读取新 image label，并允许规定的 legacy fallback。
- 每个 role/platform 的 OCI build test。
- image vulnerability 与 SBOM gate；任何 bundled Agent CLI 所在 sandbox package
  公开前，还必须显式完成 redistribution-license 决策。
- workflow policy mutation test，至少覆盖：
  - pull request 被路由到 self-hosted runner；
  - pull request 获得 package-write permission；
  - upstream job 命名 FreeInference deploy environment；
  - deployment reference 只有 tag。
- visibility 变更前，对完整历史执行 gitleaks。

### 13.2 FreeInference distribution 门禁

- 严格 manifest/lock schema，并拒绝未知 key。
- mutation case：缺 role、混合 SHA、错误 package、mutable tag、不支持 platform、
  不兼容 interface。
- staging 与 production 的 `docker compose config` 均不含 `build:`。
- 测试固定每个必须保留的 project/network/volume/state identity。
- 跨仓库 URL、callback、network 和 Web variant 一致。
- deploy/restore test：证明 `EXPECT_CLOUD_AGENT=true` 时，外部 `cloud-agent` network
  缺失会 fail closed；并证明替换后的 health gate 包含 `/agents`。
- bump-decision test：覆盖没有 CI、红色 CI、缺 image、revision 不一致、lock 已是当前版，
  以及 candidate 完整等情况。
- deployment dry-run test：证明所有 pull/validation 都发生在替换前。
- 使用两对已提交 lock/distribution 的 rollback fixture。
- 扫描 source 和生成的非 secret env，排查 secret shape。

### 13.3 Staging 验收

只有以下条件全部成立，staging 切换才算验收：

- 私有 deployment record 与实际运行 image digest 一致；
- 用户看到的 FreeInference title、link 和 `/agents` base path 不变；
- gateway frontend 已加入外部 `cloud-agent` network，且 `/agents` 能访问 lock 固定的
  Cloud Agent Web digest；
- login/callback/session renewal 正常；
- identity 与 grant 通过 live contract smoke；
- control plane 看到预期 host 与 capacity；
- 新 job 在预期 isolation 和 egress policy 下运行；
- 现有 retained chat 能用其 native state 恢复；
- workspace terminal、file、Git status、publish/no-publish control 正常；
- host replacement 不删除 retained sandbox；
- 在预期位置观察到 old-provider/label compatibility；
- rollback 与 redeploy 各成功一次；
- 之后的 upstream merge 只生成 artifact，不产生 deployment。

### 13.4 Production 验收

- production 精确使用 staging 已验收的 upstream digest set。
- distribution runtime value 为 production 值，rendered Compose 中不出现 staging
  coordinate。
- backup 和 migration classification 已记录。
- login、一次有界 inference-grant 调用、一次 no-publish agent job 和 retained-session
  access 通过。
- 新 workflow 成为权威入口后，旧 upstream production workflow 无法运行。
- last-known-good lock/distribution pair 可以立即选择。

## 14. 实施切片与依赖图

每个切片对应指定仓库中的一个可独立评审 PR：

| ID | 仓库 | 变更 | 依赖 |
|---|---|---|---|
| U0 | Cloud Agent | 本设计 | — |
| U1 | Cloud Agent | legacy deploy fuse + public workflow policy（按需 rebase #99） | U0 |
| U2 | Cloud Agent | runtime Web identity、两个 Web variant、中立 wrapper/label compatibility | U0 |
| U3 | Cloud Agent | OCI label、multi-platform build/publish、artifact policy test | U1、U2 |
| F1 | FreeInference | `cloud-agent.lock` schema、validator、renderer、manifest skeleton | U0 |
| F2 | FreeInference | digest-only Compose 与 deployment-record/rollback tooling，保持禁用 | F1、U3 |
| F3 | FreeInference | 创建 lock-only PR 的 bump bot，关闭 auto-merge | F1、U3 |
| F4 | FreeInference | staging handoff 与 smoke/rollback evidence | F2、F3 |
| U4 | Cloud Agent | 禁用/移除 direct staging CD；production 保持手动且受 gate 控制 | F4 |
| F5 | FreeInference | production handoff，并关闭旧 production gate | U4 |
| U5 | Cloud Agent | 移除剩余 production CD；最终中立化/开源清理（rebase #98/#100/#101） | F5 |
| F6 | FreeInference | 两个稳定周期后可选开启 lock-bump auto-merge | F5 |

```text
U0 -> U1 -> U3 -------┐
  \-> U2 -/           |
  \-> F1 -> F2 -------+-> F4 -> U4 -> F5 -> U5
          \-> F3 -----/                 \-> F6
```

U3 不应等待 F1 才发布 candidate，因为 F1 需要真实 candidate 来测试。F2/F3 在两侧
都具备前保持 deployment-disabled。U4 不能在 F4 前落地，否则 staging 会失去经过
验证的部署 owner。

## 15. 被否决的方案

### 保留 direct deployment，只依赖 PR runner guard

否决。上游仓库仍拥有私有 host 权限，trusted branch 配置错误或 maintainer token
失陷仍会触发部署；同时任何上游变更都会继续成为 staging 变更。

### 把 `distributions/freeinference` 放进公开 Cloud Agent 仓库

否决。把 secret value 放到别处，并没有转移 product topology、deployment history
或 release authority；这会重复 HybridInference 刚刚移除的错误边界。

### 让 FreeInference 从 upstream source checkout 构建

否决。部署字节会同时受 registry state、builder state、architecture 和 time 影响，
不再是一个经过评审的 artifact。staging 与 production 可能从同一 SHA 构建出不同结果，
回滚还需要重新构建历史版本。

### 发布一个 Web image，再由每个 distribution 重建

否决。product identity 仍然是 build input，distribution 实际运行的也不再是它测试过的
upstream artifact。runtime identity 加两个结构性 base-path variant 是更小、更诚实
的契约。

### 在 Cloud Agent 中加载可执行 distribution plugin

否决。deployment 将获得运行在应用内部的代码执行、startup ordering 与 versioning
责任，而不是只在边界提供配置。现有 typed environment setting 足够完成需求。

### 开源前重命名所有 `freeinference` identifier

否决。provider ID 和 image label 已持久化在 retained/native session state 中；Compose
name 还拥有 live resource。连续性比拼写更重要。D8 移除产品 default，但不把 machine
identifier 假装成普通品牌文案。

### 在同一个数据库上并行运行新旧 control plane

否决。scheduler、reaper、publisher 和 token transition 会让第二个 process 实际参与
系统，即使没有人向它发送 HTTP traffic。隔离 preflight 加一次受 health gate 保护的
原地切换，才是安全的对比方式。

## 16. 评审决策

本设计给出以下推荐 default；评审者必须在 U3/F1 实施前明确确认：

| 决策 | 推荐默认值 |
|---|---|
| Cloud Agent GHCR package name | 使用 §5.1 中的精确 role name |
| 迁移期间 package visibility | 保持 private，并授权 FreeInference Actions；之后与仓库 visibility 分开切换 |
| bundled CLI redistribution | public sandbox package 发布前必须有法务/license 批准；否则保持该 package 私有并记录 local build 方法 |
| Web variant | 发布 root 和 `/agents`；FreeInference 只固定 `/agents` |
| 初始 host/sandbox architecture | 其他架构完整 runtime conformance 通过前只支持 `linux/amd64` |
| lock bump auto-merge | 前两个成功 staging/production 周期保持关闭 |
| legacy provider/label 移除 | usage inventory 为零，并再经过一个完整 rollback window |
| 旧 source checkout 退役 | 两次成功私有 production deployment 且经过批准的 rollback window 后 |
| 历史 FreeInference 引用 | 准确且安全时保留，并与当前 instruction 明确区分 |

批准本设计只授权继续创建 implementation PR；不授权 deployment、merge、repository
visibility change、package visibility change、host deletion 或 compatibility-identifier
removal。这些操作各自仍需要独立评审与运营 gate。
