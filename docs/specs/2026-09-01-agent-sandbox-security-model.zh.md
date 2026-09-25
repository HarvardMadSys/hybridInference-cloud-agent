# Agent 沙盒安全模型与能力边界

**日期：** 2026-09-01

**修订：** 2026-09-03，区分已接通的安全边界与尚未关闭的部署门禁

**修订：** 2026-09-23，网络部分按[统一公网出口设计](2026-09-23-agent-public-egress-design.zh-CN.md)改为唯一网络：删除两阶段网络、网络档位与白名单代理。

**修订：** 2026-09-25，接受推理 grant 外传后的重放风险，保留现有推理入口与授权机制（§8.4）。

**状态：** 当前安全契约；Docker writable-layer quota 尚未实现，仍是 rollout gate；独立推理入口不再是发布前提，其他隔离与验收要求继续保留。

**实现仓库：** `freeinference-cloud-agent`

**适用对象：** Control plane、runner host、sandbox runtime、MCP relay、source preparer、publisher 的开发与运维人员

**相关文档：**

- [Agent 会话连续性与共享沙盒架构](2026-09-01-agent-session-continuity-architecture.zh.md)
- [仓库开发与安全边界](../../AGENTS.md)
- [部署环境与隔离档位](../../ENVIRONMENT.md)
- [Native session fork 与 shared workspace 设计](2026-08-31-native-session-fork-shared-workspace-design.md)

## 1. 安全目标

平台允许 Agent 对用户授权的仓库执行动态代码。安全设计必须满足：

1. 沙盒内代码不能获得 control plane、runner host、Docker daemon 或 source-control publisher 的控制权。
2. 沙盒只得到完成当前任务所需的短期能力；能力有 scope、subject、generation、TTL 与撤销边界。
3. Setup、Agent 执行与 Fork 使用同一套固定网络边界：只到公网 IPv4 TCP 80/443、指定解析器和本组两个中继，不到宿主、私网与特殊用途地址或其他沙盒组；没有可选择或放宽的网络模式。
4. Repository credential、长期模型 credential、MCP upstream credential 和发布 credential 不进入 workspace 或 Docker writable layer。
5. 同一 shared sandbox 内的 sibling chat 可以共享文件，但不能读取彼此 credential、控制彼此进程或接收彼此事件。
6. 安全能力无法验证时 fail closed；不得静默降级到更弱 runtime、开放网络或共享 credential 路径。
7. 观测、审计和资源采样不依赖沙盒内代码配合，也不能被其伪造为成功。

## 2. 非目标

本文不声称：

- 普通 Linux container 等同于独立内核；
- Docker 本身足以承载所有 public multi-tenant workload；
- 模型能可靠识别 prompt injection；
- Shared workspace 在 sibling chat 之间提供文件机密性；
- 安全策略可以阻止用户授权域内的 Agent 故意破坏共享代码；
- 容器隔离可以修复宿主内核、Docker daemon 或镜像供应链漏洞；
- 仅靠日志或 eBPF 可以构成访问控制边界。

## 3. 威胁模型

### 3.1 不可信输入

以下内容均按不可信处理：

- 用户仓库中的源码、脚本、Git hooks 与构建配置；
- package manager 安装脚本和传递依赖；
- README、AGENTS.md、issue、网页与检索结果中的 prompt injection；
- MCP server 返回的文本与结构化数据；
- Agent 自己生成的 shell command、patch 和后台进程；
- 编译器、测试工具、插件和 language server 的子进程；
- 通过共享 workspace 由 sibling chat 写入的文件。

一个边界如果要求 Agent “自觉不使用”某项已进入沙盒的能力，则不属于安全边界。

### 3.2 受保护资产

| 资产 | 主要风险 |
|---|---|
| 用户源码与未发布修改 | 跨租户读取、意外发布、供应链泄露 |
| Source-control access | 读取未授权仓库、push/PR 到错误目标 |
| Inference capability | 冒用用户身份、绕过模型 scope 或 quota |
| MCP upstream credential | 调用未授权工具或窃取用户第三方数据 |
| Control-plane database | 修改 job/attempt/fence、横向读取其他用户数据 |
| Runner host / Docker socket | 接管其他 sandbox 或宿主机 |
| Publisher credential | 绕过 patch gate 直接写远端仓库 |
| Sibling activation credential | 将 A 的消耗、事件或权限归到 B |
| Telemetry 与审计记录 | 隐藏安全事件或伪造归属 |

### 3.3 信任假设

Trusted Computing Base 包括：

- control plane 与数据库；
- runner、Session Supervisor 与 workspace broker；
- Docker daemon / 目标隔离 runtime；
- source preparer、gateway/MCP relay 与 publisher；
- sandbox base image、runtime pins、seccomp/capability 配置；
- host kernel，或 Kata/gVisor 提供的额外边界。

用户仓库、Agent CLI 执行的工具、workspace 内容与上游 MCP 响应不在 TCB 中。

## 4. 信任边界与数据流

```text
                      trusted control domain

 browser ──identity──> control plane ──dispatch──> runner/supervisor
                           │                         │
                           │                         ├── Docker Engine
                           │                         ├── source preparer
                           │                         └── workspace broker
                           │
                           ├── gateway: identity/model catalog/grants
                           ├── MCP registry + per-user encrypted connections
                           └── publisher + source-control credential

                                      capability boundary
                                                │
                                                ▼
                                      untrusted sandbox
                                      ├── /workspace
                                      ├── Agent CLI
                                      ├── build/test tools
                                      └── child processes

 sandbox network destinations (setup, agent and fork alike):
   1. fixed gateway relay  -> model inference only
   2. fixed MCP relay      -> selected MCP tools only
   3. public IPv4, TCP 80/443 only, plus one named resolver on TCP/UDP 53

 sandbox cannot choose a relay's upstream, and cannot reach the host, private or
 special-purpose addresses, other groups, or the control-plane listener.
```

Relay 是固定目的地转发器，不是让 caller 自选目标的通用 HTTP proxy。公网访问也不经过任何代理，由宿主规则按实际数据包放行或拒绝（§8）。

## 5. 权限所有权

| 能力 | Authority / Owner | 沙盒是否持有 | 约束 |
|---|---|---|---|
| 用户登录与角色 | Gateway identity | 否 | 短期 identity token 供 control plane 验证 |
| Job、Attempt、lease fence | Cloud Agent control plane | 否 | Runner 只持有当前 claim |
| Repository clone | Trusted source preparer | 否 | 短期 token 使用后删除 |
| Model inference | Gateway | 是，短期 `agr` capability | User、model scope、TTL、quota |
| MCP call | Cloud Agent MCP relay | 是，独立 `mcp` capability | User、job、attempt、server/tool allowlist |
| MCP upstream credential | Cloud Agent encrypted store / relay | 否 | Relay 代为调用 upstream |
| Publish/push/PR | Trusted publisher | 否 | Patch gate、secret scan、目标校验后使用短期 token |
| Docker lifecycle | Host Supervisor | 否 | Generation-fenced Docker API |
| Terminal/file/Git operation | Workspace broker | 不持有 broker authority | Membership auth + operation fence |

模型 grant 与 MCP token 必须是两个 audience。泄露 inference grant 不能调用工具；泄露 MCP token 不能调用模型或 control-plane API。

## 6. Credential 生命周期

### 6.1 禁止持久化的 credential

以下位置不得出现 clone token、inference grant、MCP token、publisher token、local-control password 或长期 provider secret：

- Docker image layer 与 `docker history`；
- container creation environment / `docker inspect Config.Env`；
- CLI argv；
- `/workspace`；
- durable `$HOME`、provider state root 或 native session transcript；
- Fork receipt journal；
- patch、artifact、日志 body 与 frontend event；
- host 的 per-chat 文件目录。

### 6.2 注入与撤销

Credential 只通过以下临时路径进入执行：

- process-scoped environment；
- membership-owned tmpfs activation directory；
- in-memory bridge local-control credential；
- Supervisor 与 relay 间的受控请求 metadata。

Activation credential 与 residency grant 共同遵守：

1. 在 generation 确认后签发；
2. 只绑定一个 subject、scope 和 execution identity；
3. 使用短 TTL；
4. 由 heartbeat 在 fence 仍有效时续期；
5. 即使 revoke 请求丢失，也会在 TTL 到期后失效。

两类 credential 的撤销边界不同：

- activation credential 绑定当前 job/attempt；turn settle、cancel 或 supersede 后撤销；
- residency grant 绑定一个 thread 的常驻 membership，Gateway external job id 为 `thread:<thread_id>`。当前 attempt 只负责证明 owner/fence 并驱动 mint/renew；一次 turn settle **不会**撤销它。只要同一个 resident process 仍 live，它可以跨该 thread 的后续 turn 保留；process replacement、idle stop、takeover 或 release 时撤销。

长 TTL + 仅依赖远程 revoke 不是可接受设计，因为 control plane 故障时 abandoned attempt 会继续持有能力。

### 6.3 Retained sandbox 与 Fork

Credential 不是 session state：

- `docker stop` 后 tmpfs 清空；
- `docker start` 后重新签发；
- Fork 不复制 credential；
- child membership 使用新的 slot、generation 与 credential；
- native session 文件可复制/分叉，但其中不得含 provider key。

## 7. Repository ingress

沙盒不直接持有 repository clone credential。流程为：

1. Control plane 校验用户、repository 与授权；
2. Trusted source preparer 在临时目录中使用短期 token fetch；
3. 删除 credential 与远端中可能携带的敏感配置；
4. 校验 tar path、file type、size、uid/gid 与目标边界；
5. 通过 Docker archive API 流入 `/workspace`；
6. 在 prompt admission 前删除临时 checkout。

Source preparer 的临时目录不是 checkpoint，也不能作为恢复或 Fork 的隐式第二份真相来源。

后续 published base 更新使用 credential-free Git bundle 导入 object；不得 reset shared worktree、index、local commit 或 uncommitted changes。

## 8. 沙盒网络边界

所有任务只使用一套网络：准备、执行与 Fork 成员共用，没有网络阶段、网络档位或代理，也没有放宽它的配置（[统一公网出口设计](2026-09-23-agent-public-egress-design.zh-CN.md)）。实现见 `cloud_agent_shared/egress.py`（配置与规则）、`cloud_agent_host/network_guard.py`（宿主 helper）与 `cloud_agent_host/chat_sandbox.py`（组网桥）。

### 8.1 拓扑

- 每个沙盒组一张可路由 Docker 网桥，从 `AGENT_EGRESS_CHAT_NETWORK_POOL` 分配 /29，接口名带本部署前缀。
- 桥上只有该组容器和两个受信中继：推理中继（网关）与 MCP 中继（MCP 专用应用）。中继在 `AGENT_RELAY_NETWORK`（`internal`，沙盒组不加入）上发现，再按别名挂到各组网桥；relay address 由 host 配置，沙盒不能指定 upstream。
- runc 容器以 `--dns AGENT_EGRESS_DNS_SERVER` 创建；Kata guest 的 `/etc/resolv.conf` 和中继的 `/etc/hosts` 条目由受信 guest-root helper 写入。
- 如果 deployment 未配置某个 relay，相关能力为空；系统应在 job admission 或 runtime launch 时拒绝不满足的请求，不能把 MCP URL 回退到 gateway。

### 8.2 宿主规则

规则是本部署自己的 nftables 表（`inet` 与 `bridge` 两个 family，表名 `cloud_agent_<前缀>`，由 `AGENT_SANDBOX_NAMESPACE` 派生）。runner 与 broker 不持有 `NET_ADMIN`，通过短时 helper 安装：同一镜像，host 网络命名空间，runc，只加 `NET_ADMIN`，只读根文件系统。沙盒不获得 `NET_ADMIN`、Docker socket 或其他特权通道。

| 类别 | 规则 |
|---|---|
| 公网 | 允许公网 IPv4 单播的 TCP 80/443 |
| DNS | 只允许 `AGENT_EGRESS_DNS_SERVER` 的 TCP/UDP 53，先于受保护网段判断 |
| 中继 | 按（网桥、地址、端口）精确放行本组两个中继 |
| 受保护目标 | 拒绝宿主所有地址，IANA 特殊用途 IPv4（私网、链路本地与云元数据、CGNAT、组播、保留等）及 `AGENT_EGRESS_PROTECTED_CIDRS` |
| 其他 | 拒绝其他沙盒组、其他端口、UDP 443、全部 IPv6 与外部主动入站；回包只接受获准出站连接的，来自受保护地址的回包仍被丢弃 |

- 先安装并回读校验规则，再创建组网桥；preflight 校验失败时 runner 不接活，组网络操作复用 60 秒内的校验结果。
- runner 进程退出不删除规则；宿主重启后，规则重新安装前不启动任何组。
- 同一宿主上两个部署的地址池重叠时，后安装的一方拒绝运行；地址池与宿主已有路由（机房网段、VPN、私有服务，默认路由与宽于 /8 的兜底路由除外）重叠时同样拒绝，因为 Docker 对显式子网不检查宿主路由，组网桥会把宿主发往这些地址的流量引入沙盒。
- 中继以网关优先级 -1 加入各组网桥，组网桥永远不会成为中继的默认出口；需要 Docker 28 及以上（客户端与守护进程），预检拒绝旧版本。
- control plane、runner API、Docker socket 与 database 的宿主或私网地址不可达；平台的公网地址可能可达，见 §8.4。

### 8.3 Guest 自检与失败行为

每次冷启动后，以沙盒用户在 guest 内检查默认路由、解析器配置并实际查询一次（根域 NS，3 秒内应答）、两个中继的 TCP 端口，以及两个受控边界探针：宿主（UDP）与元数据地址（TCP）必须被本部署规则以 ICMP“管理禁止”立即拒绝；宿主自己回“端口不可达”或无应答，说明该 guest 的流量没有经过规则。公网可达性不作为启动条件，外部故障不等于边界失效。全部通过后才执行仓库代码或安装脚本；失败则停止沙盒、保留工作区，本 turn 失败。有组在运行时，broker 至多每分钟回读一次宿主规则并补装缺失部分；连续两次仍无法证明规则时，停止本部署所有运行中的组（保留工作区与 native session），规则恢复前不再唤醒。运行中的沙盒不会在其下改接网络。旧网络拓扑留下的容器与网桥不被采用，只报告需要一次性清理。

### 8.4 接受的限制与前置条件

- TCP 80/443 只是端口约束，不检查内容，不承诺阻止 DoH 或这些端口上的隧道；
- 不防外发：沙盒可以把仓库内容和自己的短期令牌发往公网；
- 平台公网地址可能可达。MCP 工具调用只由 MCP 专用应用提供（#327，已完成），主 API 不提供工具路径；这项入口隔离继续保留。
- **2026-09-25 已接受推理 grant 的外传与重放风险**，决策见[统一公网出口设计 §3](2026-09-23-agent-public-egress-design.zh-CN.md)。仓库、依赖或外部内容可能导致 grant 泄露，第三方可在网关可达的推理入口使用它，消耗受害用户额度与平台推理资源。调用仍受所属用户的模型权限、现有配额、有效期和撤销约束，不授予其他用户身份、MCP 或平台管理权限。
- 驻留 grant 随有效进程续期，turn 结束不会撤销它；短 TTL 与默认 30 分钟空闲回收均不构成从泄露时起算的绝对有效期上限。现有停止、替换、接管和释放时的撤销及 TTL 兜底继续保留（§6.2）。
- 本决定移除独立推理入口的真实用户发布前提，保留现有 gateway、中继与 CLI 凭证注入；不要求网关改造或替身中继。它不放宽长期凭证、成员隔离、MCP、配额、Kata、网络规则或其他发布门禁。

## 9. 容器与 runtime 隔离

选择执行 runtime 时，首先要回答的不是“它是不是容器”，而是：**工具进程发出的 syscall，最后由谁执行。**

[![runc、gVisor 与 Kata 的 syscall 执行边界：普通容器直接进入宿主机内核，gVisor 由 Sentry 承接，Kata 由客户机内核执行](assets/sandbox-syscall-execution-boundaries.svg)](assets/sandbox-syscall-execution-boundaries.svg)

*图 1：隔离边界越强，宿主机越难直接观察并归因工具进程的原始 syscall。*

### 9.1 三种执行边界

#### 普通容器（runc）：共享宿主机内核

容器没有第二个内核。工具进程是宿主机上的普通进程；namespace 改变它能看见的进程、网络和挂载，cgroup 约束它能消耗的资源，但 `open()`、`mmap()`、`clone()` 等 syscall 仍由宿主机 Linux 内核直接执行。

因此普通容器启动快、兼容性完整，Host eBPF 也能直接观察工具进程的 syscall。代价是所有租户共享同一内核攻击面；namespace、cgroup、capability 与 seccomp 是必要 hardening，但不能把共享内核变成 VM 边界。

#### gVisor：用户态内核承接 syscall

gVisor 在工具进程和宿主机内核之间插入 Sentry。工具 syscall 先由 Sentry 拦截，并由其在用户态实现大部分 Linux syscall 语义；只有底层资源操作再通过一组受限 syscall 进入宿主机内核。

这显著缩小不可信代码直接触达的宿主机内核攻击面，并保留接近容器的启动特性。代价是额外的用户态路径、I/O 开销和 syscall/内核特性兼容缺口。Host eBPF 看到的是 Sentry、Gofer 等代理进程的宿主机行为，而不是工具原本发出的完整 syscall 流。

#### Kata：客户机内核执行 syscall

Kata 为沙盒创建轻量虚拟机。工具进程运行在 guest 中，syscall 由独立的客户机 Linux 内核执行；宿主机只运行 VMM，并处理 virtio、tap 等设备侧行为。

硬件虚拟化提供三者中最强的 kernel boundary，并保留接近原生 Linux 的 guest 兼容性。代价是更高的启动时间、固定内存与设备配置开销。Host eBPF 只能观察 VMM、virtio 和宿主网络设备，无法直接看到 guest 中的工具进程及其 syscall。

### 9.2 边界与观测对比

| 维度 | 普通容器 / runc | gVisor | Kata |
|---|---|---|---|
| 工具 syscall 的执行者 | 宿主机内核 | Sentry 用户态内核 | 客户机内核 |
| 与宿主机内核的边界 | namespace/cgroup/seccomp，共享内核 | 用户态 syscall implementation，缩小 host syscall 面 | 硬件虚拟化，独立 guest kernel |
| 启动与固定开销 | 最低，通常毫秒级 | 较低，通常仍是容器量级 | 最高，通常百毫秒至秒级且有 VM 固定内存 |
| Linux 兼容性 | 最高 | 有 syscall 与内核特性缺口 | 接近原生 guest Linux |
| Host eBPF 直接看到 | 工具进程的真实 syscall | Sentry/Gofer 的 host syscall | VMM、virtio、tap 等 host-side 行为 |
| Operation 归属策略 | 可按 PID/cgroup 直接关联 | 需要 runtime event 与代理侧 identity 关联 | 需要 guest agent 或 guest telemetry 回传 |

这不是“安全与可观测性只能二选一”，而是测量位置必须随隔离边界移动：runc 可以主要依赖 host sensor；gVisor 需要把 Sentry/runtime event 纳入归属；Kata 的 syscall/process 级观测必须进入 guest，host 侧只保留 VM 与设备总量。

### 9.3 当前实现与准入规则

当前 `cloud_agent_host/sandbox.py` 只定义三个 backend：`process`、`container` 和 `kata`。

- `container` 且 `runtime=None` 表示普通 runc 共享内核。Preflight 记录 `agent_sandbox_shared_kernel`，该档位只适合可信仓库或已有外层 VM 隔离的场景。
- `kata` 使用 `io.containerd.kata.v2`。Preflight 会启动 exact-image probe 执行 `uname -r`；若 guest kernel 与 host kernel 相同，则拒绝把该 host 宣告为 VM-isolated。同一 probe 还要跑 `freeinference-guest-observer --self-test`：必须经过 guest ring buffer 看到一个短命非 root child 的 fork、exec、exit 与带非零 CPU 的 `done`。BTF 对不上的 guest kernel 不能 advertise 可接活。
- 进程级观测因此走 **guest-root CO-RE observer**（`deploy/sandbox/guest-observer.*`），不是 host eBPF。Agent 进程不持有 `CAP_BPF`；helper 只由 Kata-only privileged exec 以固定 argv 拉起。详见 [Operation 可观测性](2026-09-01-agent-operation-observability-design.zh.md)。
- gVisor 当前没有独立 backend、执行档位或安全 probe。管理员能够填写 runtime 字符串不等于平台已经验证 gVisor 隔离；面向不可信仓库的强制档位当前只接受经过 probe 的 Kata。

`is_vm_isolated` 当前以“配置了非空 runtime”表达非默认 runtime，而不是独立的安全证明。安全决策不得单独依赖该字段；准入必须依赖明确的 profile、runtime allowlist 与 exact-image probe。

| 场景 | 当前最低边界 | 准入说明 |
|---|---|---|
| 一次性、隔离 VM 中的内部任务 | Hardened container | 外层 VM 承担租户 kernel boundary |
| 可信内部仓库、自托管 host | Hardened runc container | 仍需 non-root/cap/seccomp/resource limits |
| Public multi-tenant、不可信仓库 | Probed Kata VM | 当前只认 `io.containerd.kata.v2`；gVisor 需新增独立 profile 与 probe 后才能准入 |

Runtime capability 必须由 preflight 与 exact-image probe 证明。配置要求强化隔离但 host 只提供普通 Docker 时，host 不得 advertise capability，也不得 claim 对应 job。

### 9.4 基线 hardening

生产准入目标要求所有执行 profile 至少具备：

- 非 root user；
- `no-new-privileges`；
- drop all capabilities，只按审核结果增加最小集合；
- seccomp；
- CPU、memory、PID、disk 与 timeout 限制；
- `--init` 或等价 init shim；
- 无 Docker socket、host `/proc`、host cgroupfs 与 host path mount；
- credential-free immutable base image；
- 精确 runtime/helper pin 与 image digest attestation。

当前 retained/shared container create path 已设置 non-root、capability drop、`no-new-privileges`、默认 seccomp、CPU/memory/PID 与 stop timeout；它没有设置 Docker writable-layer quota。Disk limit 因而仍是明确缺口，不能把 host data-root 容量或 admission 告警写成 per-container enforcement。

## 10. Docker-owned storage boundary

Retained/shared-live 路径只通过 Docker Engine API 操作 chat state：

- create/start/stop/inspect/exec/stats/archive；
- 不读取 `/var/lib/docker`；
- 不解析 storage-driver 私有格式；
- 不维护 per-chat host workspace；
- 不为 `/workspace` 或 native state 配置 bind/named/anonymous volume。

这减少 host-path 攻击面和路径漂移，但不提供 host-independent persistence。删除 container、误运行 prune、丢失 data root 或丢失 host 仍会使 session `lost`。

在 retained-only cutover 完成前，legacy disposable runner 仍保留 host worktree/snapshot 路径；上面的 Docker-only 声明只描述已经门禁的 retained/shared-live topology，不能外推到 legacy fallback。

Session host 禁止 generic container/system prune。Disk pressure 通过 admission 与告警处理，不能通过静默删除 retained sandbox 处理。

## 11. Shared sandbox 的 member 隔离

Fork sibling 属于同一个用户/security domain，并有意共享 `/workspace`。Credential 与 process control 仍是硬边界。

### 11.1 Execution slot

每个 membership 使用预制 slot：

```text
member A: uid 11001, shared gid, private tmpfs A, process generation PA
member B: uid 11002, shared gid, private tmpfs B, process generation PB
workspace: shared setgid group, no credential files
platform journal: sandbox owner, credential-free
```

具体 UID 只是实现参数；安全契约是：

- sibling UID 不可读取 `/proc/.../environ`；
- 不可 ptrace 或 signal sibling；
- 不可读取 sibling tmpfs/config；
- slot reuse 前必须完成 secret/process cleanup；
- 无安全的 UID/slot pool 时不 advertise shared capability。

### 11.2 Shared state 与 private state

| 内容 | Shared | Private |
|---|---|---|
| `/workspace` 与 `.git` | 是 | 否 |
| 依赖、构建产物、项目级 instruction | 是 | 否 |
| Native conversation id | 否，per membership | 是 |
| Provider home | 视 runtime；并发不安全者 per member | 是 |
| Activation credential/config | 否 | 是，tmpfs |
| Event stream 与 cancellation | 否 | 是，generation-fenced |
| Terminal filesystem effect | 是 | Terminal ownership 与 cancel 是 per member |

文件由 sibling 故意 chmod、删除或覆盖属于 shared-workspace interference，可能导致某个 member `lost`；它不应因此获得 sibling credential。

### 11.3 Loopback service

在同一 container network namespace 中，`127.0.0.1` 不是 membership 边界。使用 loopback HTTP server 的 runtime 必须：

- 绑定随机端口，不发布到 host；
- 使用 process-generation scoped authentication；
- credential 只存在 bridge process environment/memory；
- 未认证请求返回拒绝；
- sibling 不可从 `/proc` 或持久文件读取 credential。

“只监听 localhost”本身不足以隔离 sibling process。

## 12. 进程生命周期与取消

安全清理不能依赖 Agent CLI 正常退出。

### 12.1 Turn boundary

Turn 开始前记录 member UID 的 PID + start-time baseline。Turn 结束或取消时：

1. 优先调用 runtime-native interrupt；
2. 对本 activation 新增的同 UID descendants 发送 TERM；
3. 超时后 KILL；
4. 重复 census，处理 `setsid`、double-fork 与 PID reuse；
5. 仅在 zero-descendant proof 后把 resident root 标为 idle/HOT。

Process group 只是一条线索，不是完整边界。PID 必须与 start time 组合，避免 PID reuse 误杀。

### 12.2 不明确的取消边界

Prompt admission 前取消可以安全释放 attempt。Prompt admission 后，如果 runtime 无法证明 prompt 未被消费或已经到达 settled boundary：

- 停止该 membership transport；
- 标记 member `lost` 或明确 dirty failure；
- 不 replay 同一 prompt；
- 不停止 sibling process 或整个 group，除非 container 本身失效。

## 13. File、Terminal 与 Git API

这些 API 必须通过 workspace broker，而不是 host filesystem：

- 路由先授权 `user -> thread -> membership -> sandbox`；
- 每个 mutation 创建 generation-fenced operation token；
- operation 参与 group activity/refcount，防止 idle stop；
- file path 规范化并限制在 workspace root；
- archive upload 校验 path traversal、symlink 与 file type；
- command/PTY 具有 idle timeout 与 hard lifetime；
- terminal 没有 inference/MCP credential；
- terminal ownership 与 cancellation per membership；
- Git view 可共享，但 publisher authority 不在 broker 内。

Read-only operation 可在 sibling turn 期间继续；writer operation 必须被 snapshot quiescence census 计入。

## 14. Publication boundary

沙盒不能 push。发布路径为：

1. 沙盒内部生成相对可信 base 的 patch；
2. Control plane 存储 immutable artifact 与 workspace epoch；
3. Trusted publisher 在外部 checkout 应用 patch；
4. 校验 path、mode、symlink、binary policy 与 patch base；
5. 执行 secret scan 与 workflow approval policy；
6. 使用短期 source-control credential commit/push/PR；
7. 外部副作用结果不明确时停止自动重试。

Publisher 不能信任沙盒声明的目标 repository、branch 或 base；必须从 control-plane authority 重新解析。

## 15. Fail-closed 规则

| 检查失败 | 禁止行为 | 必须结果 |
|---|---|---|
| Identity / authorization 不可判定 | 创建 job | 4xx/5xx，不能匿名降级 |
| Runtime/image attestation 失败 | Claim shared/untrusted job | Host 不 advertise capability |
| Gateway grant 无法验证 | Model call | 401/403/503；不得 unmetered 通过 |
| MCP token/fence 无法验证 | Tool call | Relay 拒绝 |
| 宿主网络规则缺失或回读校验失败 | 创建组网桥、claim job | Host 不接活，组网络操作失败 |
| Guest 网络自检失败（路由、解析器、中继） | 执行仓库代码或发送 prompt | 停止沙盒、保留工作区，turn 失败 |
| Native resume 不可证明 | 发送 follow-up | `lost`，不 blank-session fallback |
| Slot cleanup 不明确 | Reuse slot | Member fail closed |
| Snapshot writer census 不一致 | 宣称 quiescent/publish | 保持 pending/dirty |
| Publisher external result 不确定 | 自动重推 | 等待人工 reconcile |
| Required strong isolation 不可用 | 普通 Docker fallback | 拒绝 job |
| Kata guest observer `--self-test` 失败 | 宣称该 host 可接 Kata 活 | Host 不 advertise capability |

## 16. 审计与观测

安全相关记录至少包括：

- Job/Attempt/lease/generation transition；
- Grant mint/renew/revoke/expiry，不记录 token；
- 宿主网络规则的安装与回读校验、guest 网络自检与 relay probe；
- 不做逐连接出站审计（统一出口设计 §10）；宿主规则上的拒绝计数是本部署的总量，不归属到组或成员；
- Slot allocation、process cleanup 与 ambiguity；
- Native resume/Fork receipt validation；
- Container OOM、unexpected exit、missing object；
- Snapshot gate、secret scan、publisher result；
- Sampler coverage 与 sensor gap；
- Kata guest observer 的 cursor/loss/incomplete 标记，不是“完整的零进程普查”。

日志属性只使用 bounded identifiers/enums。Prompt、tool argument/result、credential、repository token 与完整环境变量不得进入共享 telemetry sink。

## 17. 验收矩阵

### 17.1 Credential

- `docker inspect`、image history、workspace、native state 与 Fork journal 中无 credential；
- revoke 丢失时短 TTL 仍会终止能力；
- suspend user 后 model call 立即拒绝；
- inference token 不能访问 MCP，MCP token 不能访问 inference/control plane；
- sibling slot 不能读取 credential 或 local-control password；
- slot reuse 前不存在旧 secret 或 process。

### 17.2 Network

按[统一公网出口设计](2026-09-23-agent-public-egress-design.zh-CN.md) §9 的验收表执行，要点：

- 实际 Kata guest 中 curl、Python HTTPS、HTTP 80、包下载与 Git HTTPS 可用；
- 其他公网端口、UDP 443 和其他解析器被拒，指定解析器正常；
- 宿主各地址、私网、链路本地/元数据、CGNAT、组播、保留地址、追加保护网段与其他沙盒组不可达，DNS 重绑定后的私网连接同样被拒；
- 本组两个中继的服务端口可用，其他端口被拒；gateway/MCP relay 只能到固定 upstream；
- IPv6 与外部主动入站被拒；
- 规则、DNS 与 guest 自检完成前不执行仓库代码；准备到执行沿用同一网桥，没有网络阶段切换；
- 未配置 relay 时请求明确失败。

### 17.3 Runtime isolation

- non-root、cap drop、seccomp 与 resource limit 在真实 runtime 生效；
- public multi-tenant profile 在当前实现中通过 Kata exact-image probe；新增 gVisor 等后端前必须提供独立 profile、runtime allowlist 与等价 probe；
- sibling `/proc`、ptrace、signal、tmpfs 测试通过；
- `setsid`、double-fork、ignore TERM 能被清理；
- container OOM 观察为 group failure；
- 无静默 fallback 到共享 UID 或普通 Docker。

### 17.4 Data 与 publication

- Clone token 不进入 sandbox；
- tar/archive path traversal 与异常 file type 被拒绝；
- shared workspace race 在 UI/API 中明确，不虚构 per-chat patch；
- patch gate、secret scan 与 target validation 在 trusted publisher 中执行；
- ambiguous push 不自动重复；
- retained container 不被 generic prune 删除。

## 18. 已知剩余风险

- Docker writable layer 仍是单一持久副本；host/data-root 丢失导致 `lost`。
- 当前没有 per-container writable-layer quota；disk-pressure admission 不能替代资源隔离。
- 公网出口按端口放行，不检查内容，也不防外发；推理 grant 被带出后可在有效期间按所属用户权限重放，已明确接受该风险。MCP 的公网入口隔离继续保留（§8.4）。
- Shared workspace 允许同一授权域内的 sibling 相互影响文件和项目级 state。
- 普通 Docker 共享宿主内核，只适合相应信任等级。
- Runtime-native state format 与并发行为受精确版本影响，升级必须重新 attestation。
- Relay 与 publisher 扩大 TCB，需要独立 patch、依赖和部署审计。
- Resource/operation sensor 故障不会停止 turn；必须通过 coverage 与 ops event 暴露，而不能误报为零风险。

## 19. 实现导航

| 边界 | 主要路径 |
|---|---|
| Sandbox 与 Docker lifecycle | `cloud_agent_host/sandbox.py` |
| 沙盒网络：配置、宿主规则与组网桥 | `cloud_agent_shared/egress.py`、`cloud_agent_host/network_guard.py`、`cloud_agent_host/chat_sandbox.py` |
| Source setup/import | `cloud_agent_host/setup.py` |
| Supervisor、slot 与 process cleanup | `cloud_agent_host/shared_session_supervisor.py` |
| Runtime credential/config | `cloud_agent_host/runtimes.py`、`cloud_agent_host/resident_transport.py` |
| Workspace broker | `cloud_agent_host/workspace_broker.py` |
| MCP registry/relay/proxy | `cloud_agent/mcp_registry.py`、`cloud_agent/api/mcp_relay.py`、`cloud_agent/mcp_proxy.py` |
| Inference grant client | `cloud_agent/grants.py`、`cloud_agent/gateway.py` |
| Publisher | `cloud_agent/publisher.py`、`cloud_agent/publish_worker.py` |
| Resource sampling | `cloud_agent_host/resources.py` |
| Kata guest eBPF observer | `deploy/sandbox/guest-observer.{bpf.c,c,h}`、`cloud_agent_host/guest_observer.py` |

任何放宽 network、credential、UID、capability、seccomp、publisher 或 Docker API 边界的变更，都需要安全评审与对应 acceptance test，不能作为普通兼容修复静默落地。
