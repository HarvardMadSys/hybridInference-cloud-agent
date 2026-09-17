# Admin → Servers：存储容量展示设计

- 状态：**已实现**，代码见同一分支上的两个 commit（PR 1 探针→API、PR 2 Servers 展示），实现记录见文末。2026-09-16 按评审重写，同日第二次修订验证逻辑（第 4.3、5 节）。
- 需求：[Issue #232 — Servers cannot say how full the Docker data-root is](https://github.com/HarvardMadSys/hybridInference-cloud-agent/issues/232)。
- 前序：[PR #189](https://github.com/HarvardMadSys/hybridInference-cloud-agent/pull/189)、[服务器容量 v1 设计](2026-09-11-admin-server-capacity-design.zh.md)。
- 核查基线：本地 `3a191e5`；实现时重新核对 `dev`。

## 1. 要解决的问题

管理员目前能看到 CPU、内存、执行槽位和 retained sandbox 数量，但回答不了：**沙箱数据所在的盘用了多少、还剩多少。**

`25 stopped` 只说明容器停止后仍被保留。仓库、依赖、未提交修改和原生会话都在容器的可写层里；停止不释放空间，数量也推不出磁盘压力。

本期交付一个指标：**沙箱可写层所在文件系统的容量**。主表在 Memory 后增加 Storage 列，显示使用率、已用／总量、可用空间，以及缺测原因和观测年龄。

本期不做：历史曲线、逐沙箱或逐用户归因、镜像与缓存拆分、inode、配额、清理、告警、Drain、准入调整、增长预测、控制面数据库所在盘。这些各自独立设计；本功能不改变保留沙箱的生命周期。

## 2. 设计决定

| 问题          | 决定                                                                                | 理由                                                                                        |
| ------------- | ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| 指标定义      | 沙箱可写层所在文件系统的块空间，df 口径                                             | 这是 retained sandbox 真正占用的盘；名字里写清统计对象                                      |
| 接口边界      | 探针在 host 采集器内可替换；心跳、控制面、页面只认合同                              | 换服务器或换存储后端只换探针                                                                |
| 首个探针      | runner 对自身 `/` 做 `statvfs`                                                      | OverlayFS 的 statfs 返回 upper 层所在文件系统的数据；runner 与沙箱共用同一 daemon 和 driver |
| 验证对象      | 宿主上包含 runner upperdir 的那个文件系统的**挂载根**                               | 挂载根不受目录级 project quota 影响；`df upperdir` 会和容器一起读到配额                     |
| 验证状态传递  | 部署脚本写 verdict 文件，只读挂进 runner；未验证或失效时只报 `unverified`，不报数值 | 采集器先于部署检查启动，脚本报错拦不住未验证的数值进页面                                    |
| 部署影响      | 存储验证失败不阻断部署                                                              | 监控不能挡任务；失败以缺测原因呈现                                                          |
| 历史          | 首期只做当前值，复用 JSONB 快照                                                     | 不加列、不加 migration；分钟级 24 小时曲线不是这个需求的核心                                |
| 协议          | `schema_version` 保持 1，加可选 `storage` 子块                                      | 新旧 runner 和控制面可交错升级                                                              |
| 隔离          | 在现有 collector 周期内采样，不起子进程                                             | 本地文件系统的 `statvfs` 是一次系统调用；诚实边界见第 6 节                                  |
| 多 enrollment | 沿用 `capacitySource` 的选源                                                        | 与 CPU、内存同一来源；不新增多来源界面，也不把多来源当配置错误                              |

本文默认的两个取舍，评审时可改：子块名用 `storage` 而非 `sandbox_storage`；`target` 取 verdict 里记录的宿主挂载根，只在已验证时出现。

## 3. 接口

### 3.1 合同与验收

合同一句话：

> 本 host 上保存 retained sandbox 可写层的那个文件系统，块空间的总量、空闲、可用，按 df 口径。

任何探针满足这句话即合法。验收方式唯一：在那台机器上，探针的三个数与宿主对该文件系统**挂载根**执行 `df` 的结果一致（第 5 节）。换服务器就是换探针、跑一遍对照；其余各层不动。

### 3.2 host 探针接口

采集器只依赖这个接口，不知道背后是 overlay、卷、宿主写文件，还是以后其他主机类型上的目录 `statvfs`。

```python
class StorageProbe(Protocol):
    source: str  # 方法标识，如 "linux_overlay_root_statvfs"

    def sample(self) -> StorageSample | StorageMissing: ...


@dataclass(frozen=True)
class StorageSample:
    total_bytes: int
    free_bytes: int
    available_bytes: int
    target: str | None  # 给人看的标签；不参与任何逻辑


@dataclass(frozen=True)
class StorageMissing:
    reason: Literal["disabled", "unsupported", "unverified", "source_unavailable"]
```

用哪个探针由部署声明：`AGENT_HOST_STORAGE_PROBE=overlay_root | off`。部署脚本知道拓扑，采集代码不猜。新探针是新的取值和新的 `source`，不是新的协议版本。

首期不带文件系统身份字段。OverlayFS 在部分配置下会把 `f_fsid` 换成自身 UUID 派生的值，它不是底层盘的指纹（[内核实现](https://github.com/torvalds/linux/blob/master/fs/overlayfs/super.c#L258)）；没有历史曲线也就没有连续性需求。若以后做历史，身份由部署注入，不在容器内推导。

### 3.3 心跳子块

放进现有 `capacity` 对象，与 CPU、内存并列。顶层 `schema_version: 1` 和 `source` Literal 不变。

```json
{
  "storage": {
    "source": "linux_overlay_root_statvfs",
    "target": "/mnt/agent-docker",
    "total_bytes": 107374182400,
    "free_bytes": 42949672960,
    "available_bytes": 37580963840
  },
  "missing": { "cpu": null, "memory": null, "storage": null }
}
```

- `storage.source`：匹配 `^[a-z0-9_]{1,64}$` 的标识串，控制面**不做枚举**。新探针不需要控制面发版。
- `storage.target`：可选，最长 256 字符，拒绝控制字符，只进 tooltip。首期取 verdict 记录的宿主挂载根，即数值实际来自的那个文件系统；不填 `AGENT_RETAINED_DOCKER_DATA_ROOT`，因为可写层可以在另一块盘上。
- 三个字节字段为严格整数，`0 <= available <= free <= total <= 2^53 - 1`；不接受布尔值、NaN 或字符串自动转换。
- 子块可选。旧 runner 不带时，admin 视图记 `storage=null`、`missing.storage=not_reported`。
- 有效 `storage` 与非空 `missing.storage` 互斥，与 CPU、内存现有规则一致。
- `missing.storage` 的词表与探针无关：

| 原因                 | 谁产生 | 含义                                       | 界面                               |
| -------------------- | ------ | ------------------------------------------ | ---------------------------------- |
| `not_reported`       | 控制面 | runner 或 API 没有该能力                   | Not measured                       |
| `disabled`           | runner | `AGENT_HOST_STORAGE_PROBE=off`             | Storage measurement disabled       |
| `unsupported`        | runner | 探针条件不满足，或部署验证判定该拓扑不支持 | Storage not supported on this host |
| `unverified`         | runner | 部署验证尚未通过、缺失或已失效             | Storage not verified               |
| `source_unavailable` | runner | 本轮系统调用或文件读取失败                 | Not measured                       |
| `invalid_sample`     | 控制面 | 子块存在但校验失败                         | Invalid sample                     |

控制面在 `HostCapacityReport` 里局部校验子块：失败时仅置空 Storage 并记 `invalid_sample`，不丢 CPU、内存、inventory，更不拒绝心跳。`admin_capacity_view` 读已存快照时走同一模型，旧快照自然成为 `not_reported`。8 KiB 总上限不变。

### 3.4 控制面与 API

`AgentHostCapacity` 增加：

```text
storage_total_bytes        int | null
storage_free_bytes         int | null
storage_available_bytes    int | null
storage_used_bytes         int | null    # 派生
storage_used_percent       float | null  # 派生
storage_source             str | null
storage_target             str | null
storage_missing_reason     str | null
```

派生值由一个纯函数 `storage_usage` 计算，模式与 `memory_usage` 相同：

```text
used         = total - free
used_percent = 100 × used / (used + available)
```

`free` 含保留块，`available` 不含，两者都要保留，不能用 `total - available` 冒充已用。分母为零（全部保留且 `used` 为 0）按 `invalid_sample` 处理。API 返回未取整百分比；主表向上取整，与 GNU `df` 对齐。

历史链路不变：`agent_host_capacity_history` 不加列，分钟采样的 JSONB 投影只读固定路径，多出的子块被忽略。

### 3.5 页面

页面只读 DTO，永远不按 `storage_source` 分支。`source` 和 `target` 只出现在 tooltip。

## 4. 首个探针：runner 的根文件系统

### 4.1 为什么是 `/`

runner 是同一个 Docker daemon、同一个 storage driver 下的普通容器，根文件系统是带 upper 层的 overlay。内核 `ovl_statfs` 对根目录取 real path，有 upper 时即 upper 所在路径，再对它调用 `vfs_statfs`；容量数字因此来自 upper 所在的文件系统。[内核实现](https://github.com/torvalds/linux/blob/master/fs/overlayfs/super.c#L250)

runner 的 upper 目录和沙箱的可写层由同一个 driver 放在同一个目录树下：classic overlay2 是 `<DockerRootDir>/overlay2/`，containerd image store 是 snapshotter 的目录，后者可以在独立的目录和磁盘上（[Docker 文档](https://docs.docker.com/engine/daemon/#daemon-data-directory)）。两种情况下量到的都是沙箱可写层所在的盘，正是合同要求的对象；验证也只认这个盘，不认 DockerRootDir。

采样就是一次 `os.statvfs("/")`。不需要新卷、新挂载、Docker 查询或宿主挂载表。

### 4.2 运行时条件

每轮采样前读 runner 自己的 `/proc/self/mountinfo`：挂载点为 `/` 的记录，文件系统类型必须是 `overlay`，super options 必须含 `upperdir=`。不满足则报 `unsupported`。这是一条记录的查找，不是通用 mountinfo 解析器。upperdir 路径同时作为本 runner 在 verdict 里的键（4.3）。

`statvfs` 结果不满足 3.3 的范围时报 `source_unavailable`。

### 4.3 部署验证与状态传递

**为什么不能拿 `df upperdir` 对照。** Docker 的 `overlay2.size` 或容器的 `--storage-opt size=` 要求 backing filesystem 为启用 pquota 的 XFS，通过 project quota 实现（[dockerd 配置](https://docs.docker.com/reference/cli/dockerd/#overlay2size)）。XFS 对受 project quota 约束的目录，`statfs` 返回该项目的限额而非磁盘（[XFS 实现](https://github.com/torvalds/linux/blob/master/fs/xfs/xfs_qm_bhv.c#L56)），ext4 的 `ext4_statfs_project` 同理；GNU `df` 收到目录参数时就对该路径查询（[GNU df 实现](https://github.com/coreutils/coreutils/blob/master/src/df.c#L1319)）。于是磁盘 1 TiB、配额 10 GiB 时，容器 `/` 和宿主 `df upperdir` 都读到 10 GiB，设备号也相同，对照形同虚设。

**对照对象是挂载根。** 宿主用 `findmnt -T <upperdir>` 找到包含 upperdir 的文件系统及其挂载根；挂载根不在任何容器目录的 project 之内，`df <挂载根>` 才是磁盘本身。同一文件系统上，`df upperdir` 与 `df <挂载根>` 的 `size` 不等，就是配额生效的直接证据。

**验证在 `check_shared_live_stack` 里对每个副本进行**，与现有 `docker exec … python -c` attestation 同一步：

1. 在 runner 内执行探针自己的描述命令，输出 `fstype`、`upperdir` 和三个数；与运行时采样共用代码。
2. `findmnt -n -o TARGET,FSTYPE,SOURCE -T "$upperdir"` 得到挂载根。失败记 `unverified`。
3. 配额：`df -B1 --output=size "$upperdir"` 与 `df -B1 --output=size "$mount_root"` 必须相等，否则记 `unsupported`（project quota）。另外尽力核查配置：`daemon.json` 的 `storage-opts` 或 dockerd 参数含 `overlay2.size` 也记 `unsupported`，即使前一项碰巧相等。
4. 容器 `total_bytes` 必须与 `df -B1 --output=size "$mount_root"` **完全相等**。`size` 不受并发写入影响，是硬性标准；不等记 `unsupported`，并记录两边数值。
5. `used`／`avail` 只做诊断，不是拓扑判据：最多三轮，容器读数与任一宿主读数之差在 `max(1 GiB, size × 1%)` 内即可；始终超出则记 `unverified`，附上观测到的差值。占用不单调，构建与清理可以交错，因此不用前后夹取当硬性标准。
6. 把结果写进 verdict 文件。存储验证的任何失败只写 verdict 和警告日志，**不加入部署的致命 verdicts，不阻断部署**。`AGENT_HOST_STORAGE_PROBE=off` 时跳过。

**非 root 部署用户的验证路径。** `findmnt -T upperdir` 需要遍历 snapshot 目录，默认的 `/var/lib/containerd` 是 root-owned 0700；Docker socket 可用不代表宿主 shell 可以遍历它。直接读取失败时，部署从 `findmnt --kernel --list --json` 的挂载表选择候选挂载，再用当前 runner 的不可变 image ID 启动一次性 `storage_attestation` helper：

- 仅将候选挂载根和当前 upperdir 作为两个**非递归、只读**目录 bind，目标固定为 `/disk` 和 `/upper`。这是部署阶段的受控例外；运行时采集器和 retained sandbox 不增加宿主目录挂载。
- helper 读取自己的 fdinfo / mountinfo，以打开目录的 mount ID 精确定位实际 bind，核对设备号、文件系统内根路径和类型与候选计划一致。只做字符串前缀匹配不能签发 verified；软链接、遮蔽挂载或拓扑变化导致不一致时拒绝读数。
- 两个路径各做一次 `fstatvfs`，返回挂载根的 total / used / available 和 upperdir 的 total；其后仍走同一套配额、总量和占用容差判定。占用重试会刷新宿主读数。
- `--network none`、`--cap-drop ALL`、`no-new-privileges`、只读容器根、资源限制、`--pull=never`；不挂 Docker socket、宿主 proc 树或凭据，不用 privileged / host PID / sudo，也不改变宿主目录权限。bind 使用 `rslave`，满足 Docker 对覆盖 daemon root 的目录挂载的要求，传播方向仅为宿主到 helper。
- Docker 调用共享原来的剩余预算；helper 自身另有最多 5 秒的 SIGALRM，以免 CLI 被终止后留下继续运行的探针。正常结束由 `--rm` 删除，启动失败或超时在剩余预算内尝试清理。无法读取时仍为 unverified，不阻断部署。

**verdict 文件。** deploy 用户拥有 checkout 根下的专用目录 `.storage-probe/`，文件为其中的 `verdict.json`。`sandbox_host.sh` 在 `up` 之前准备目录和空 entries，验证后写回结果；Compose 以 `create_host_path: false` 将**目录**只读挂到 `/run/cloud-agent/storage-probe`。每轮重新打开 `verdict.json`，因此文件删除、重建、rename 替换都会在下一轮可见。保持挂载目录本身不变，不暴露 checkout 或其他凭据。旧单文件挂载需要通过正常 `up` 重建 runner 完成升级，单独 `check` 不能改变已创建容器的挂载。内容上限 16 KiB：

```json
{
  "schema_version": 1,
  "written_at": "2026-09-16T08:00:00Z",
  "deployment_sha": "3a191e5",
  "entries": {
    "/mnt/agent-docker/overlay2/9f1c…/diff": {
      "verdict": "verified",
      "mount_root": "/mnt/agent-docker",
      "fstype": "xfs",
      "size_bytes": 107374182400,
      "reason": null
    }
  }
}
```

**runner 每轮的判定顺序**：

1. `AGENT_HOST_STORAGE_PROBE=off`：`disabled`，不读文件。
2. `/` 不是带 upperdir 的 overlay：`unsupported`。
3. verdict 文件缺失、不可读、超限、非法 JSON：`unverified`。
4. 没有本 runner 当前 upperdir 的条目：`unverified`。容器被重建后 upperdir 变了，旧 verdict 自然失效。
5. 条目 verdict 不是 `verified`：按条目里的 `unsupported` 或 `unverified` 上报。
6. 条目 `size_bytes` 与本轮 `statvfs` 的 `total` 不等：`unverified`。文件系统扩容或后端变化后，需要重新验证。
7. 以上都通过：上报三个数，`target` 取条目的 `mount_root`。

没有基于时间的过期：绑定 upperdir 与 `size` 就是失效规则。`sandbox_host.sh up` 每次重新验证；`sandbox_host.sh check` 也走 `check_shared_live_stack`，可在不重新部署时刷新 verdict。

### 4.4 已知边界

- 测的是块空间。`available` 不保证某个容器还能写：inode、配额或只读故障仍会失败。界面不称之为“沙箱剩余额度”或“主机健康”。
- 它包含同文件系统上其他目录的占用，不等于 Docker 对象大小之和，也推不出还能开多少沙箱。
- 挂载根本身若被设了带限额的 project quota，`df` 无法区分；第 3 步的配置核查是尽力而为。这种配置下读数仍是该文件系统上所有写入者看到的容量。
- containerd image store 把快照放在独立磁盘时属于支持范围：量的是 snapshotter 所在盘，`target` 指向它。
- rootless、remote Docker、btrfs／ZFS、devicemapper 未验证；验证不通过就是不支持，不猜。
- 本地开发（Docker Desktop）：宿主 shell 看不到 Linux VM 的挂载，无法做 attestation，Storage 显示 `unverified`。前端用 fixture 开发。

## 5. 验收实验

实现前先在一台 Linux 验证主机上跑一次，记录 Docker 版本、storage driver、image store 模式、backing filesystem、挂载关系。**必须在实际 runner 里测**，对照它自己的 upper 层所在文件系统的挂载根，不能固定比较 `<DockerRootDir>/overlay2`。

**静止对照。** 停掉构建和其他写入后执行：

```bash
id="$(docker ps -q -f name=runner | head -1)"
upperdir="$(docker exec "$id" awk '$5 == "/"' /proc/self/mountinfo | sed -n 's/.*upperdir=\([^,]*\).*/\1/p')"
mount_root="$(findmnt -n -o TARGET -T "$upperdir")"
findmnt -n -o TARGET,FSTYPE,SOURCE -T "$upperdir"
df -B1 --output=size,used,avail,target "$mount_root"
docker exec "$id" python -c 'import os; s = os.statvfs("/"); print(s.f_frsize * s.f_blocks, s.f_frsize * (s.f_blocks - s.f_bfree), s.f_frsize * s.f_bavail)'
df -B1 --output=size,used,avail,target "$upperdir"
```

通过标准：静止状态下容器三个数与 `df "$mount_root"` 逐项相等；`df "$upperdir"` 的 `size` 与挂载根相等。无法做到静止时重复取样，直到出现完全相等的一组；始终不等则记录差值范围，标记为未验证，不硬判。

**沙箱同盘确认。** 任选一个 retained sandbox 容器，取其 upperdir（overlay2 用 `docker inspect` 的 `GraphDriver.Data.UpperDir`；containerd 模式按 snapshotter 路径），`findmnt -T` 必须给出同一个挂载根。这证明 runner 的读数就是沙箱可写层的盘。

**反例 A，配额，必须真正跑通。** 在 backing filesystem 为带 `pquota`／`prjquota` 的 XFS 的主机上，用 runner 镜像起一个 `--storage-opt size=10G` 的测试容器。预期：容器 `statvfs` 的 `total` 为 10 GiB；宿主 `df upperdir` 同样为 10 GiB，这正是 4.3 描述的漏检；宿主 `df mount_root` 为磁盘容量；attestation 第 3、4 步记 `unsupported`。三组数都写回本文。

**反例 B，非 overlay 根。** 用 `vfs` driver 的测试 daemon 或 `--rootfs` 启动探针，预期 4.2 报 `unsupported`。

**正例 C，分盘。** data-root 或 containerd snapshotter 在独立磁盘上，预期 `findmnt -T upperdir` 指向该盘，验证通过，`target` 是该盘的挂载根而不是 `/`。

**2026-09-16 真机验收更新：** [完整验收记录](../acceptance/2026-09-16-admin-storage-capacity.md)。在真实 Linux / Docker 的隔离环境中，XFS project quota 反例 A、vfs 反例 B、独立 loop-backed XFS 文件系统正例 C 均通过；A 中容器和 `df upperdir` 都返回 `10737418240 B`，挂载根返回 `33939488768 B`，验证正确拒绝配额读数。C 中容器、upperdir 和挂载根三个数逐项相等，target 指向 `/lab/disk/docker`，不是外层根文件系统。

**修复后核心验收通过（`a2e2e36`）。** 首次 `f425811` 因非 root 部署用户无法遍历 0700 的 containerd 快照目录而只能显示 unverified。新增受控 helper 后，三个 staging 副本均 verified，宿主 `df` 与实际 runner 三个数逐项相等，admin API 和页面显示正常 TiB 容量；已停止 retained sandbox 的 upperdir 也确认与 runner 同盘。新 helper 以非 root 调用者、实际 staging 镜像重跑 XFS A / C，仍正确拒绝配额、接受独立文件系统。原地清空 verdict 条目后的局部降级及 `check` 恢复也已实测。目录权限未改；完整数值、截图及尚未强制构造的场景见验收记录，不代表所有生产拓扑均已验证。

## 6. 采样与故障边界

- 在现有 collector 的 30 秒后台周期内采样，与 CPU、内存同一份 `HostMeasurement`，同一个 `sample_id` 和样本年龄。
- 每轮读一次 verdict 文件，上限 16 KiB，非法内容按 `unverified`；不缓存“曾经验证通过”。
- 不起独立子进程，不设单独预算。本地 ext4／XFS 的 `statvfs` 读内存计数器，通常不做磁盘 I/O。
- 诚实边界：极端情况下（文件系统被 freeze、设备故障）这一次调用可能阻塞，整份容量样本会因此过期，页面按现有年龄机制显示 stale；心跳线程和任务执行不受影响。本文不同时宣称“无隔离”和“其他指标绝不受影响”。
- 存储失败只影响本轮 `missing.storage`；不把上一轮成功值复制进新样本。

## 7. 页面行为

- 列顺序：`Server / Status / Execution slots / CPU / Memory / Storage / Sandboxes / Observed / Actions`；展开行跨度改为 9。
- `storageCell` 与 `memoryCell` 同构：百分比、已用／总量、第二行可用空间、缺测原因文案。已用／总量按总量选 GiB 或 TiB；可用空间独立选单位，避免小余量显示为 `0 TiB`。
- 来源：沿用 `groupCapacityFacts` 里 `capacitySource` 选中的 enrollment，与 CPU、内存同源。多个部署共享一个物理名时不求和、不求最大值；来源不一致的现有提示同样覆盖 Storage。选中的 enrollment 是旧 runner 时显示 Not measured，这只在滚动升级期间出现。
- tooltip：指标定义一句话、`used / (used + available)` 口径、保留空间说明、`target`（“the filesystem mounted at …”）、`source`。`unverified` 时说明需要一次成功的部署验证。
- 不新增曲线，不改 `CapacityPanel` 和 `CapacityMetric`。
- 说明文字补一句：Storage 与 Memory 一样是瞬时观察。

## 8. 实现拆分与发布

**PR 1：探针到 admin API。** `cloud_agent_host/host_capacity.py` 增加探针接口、`overlay_root` 实现、verdict 读取与描述命令、`HostMeasurement` 与 `as_payload` 扩展；`cloud_agent/capacity_protocol.py` 子块校验、`storage_usage`、DTO；`deploy/host/sandbox_host.sh` 验证与 verdict 写入；`deploy/host/docker-compose.sandbox.yml` 只读挂载；`ENVIRONMENT.md`、`docs/operations.md`、`deploy/host/.env.example`。先让 API 可读到真实数据。

**PR 2：Servers 展示。** `web/src/lib/api/admin.ts` 类型、`storageCell`、表头与跨度、tooltip、测试与截图。可先用 fixture 开发。

配置只加一个：`AGENT_HOST_STORAGE_PROBE`，host stack 默认 `overlay_root`。`AGENT_HOST_CAPACITY_ENABLED=0` 仍关闭整个采集器。

升级顺序：控制面，然后 host stack／runner，然后前端。没有 migration。verdict 文件与 `.shared-live-ready` 一样由脚本在 `up` 前创建，旧脚本部署新 runner 时文件缺失，Storage 为 `unverified`。

| 组合                  | 行为                                                      |
| --------------------- | --------------------------------------------------------- |
| 新 runner，旧控制面   | 旧模型忽略未知字段；CPU、内存、inventory 不变             |
| 旧 runner，新控制面   | Storage 为 `not_reported`                                 |
| 新 runner，旧部署脚本 | 无 verdict，Storage 为 `unverified`                       |
| 新前端，旧控制面      | 字段 `undefined` 归一为缺测，显示 Not measured，不显示 0% |
| 旧前端，新控制面      | 忽略额外字段                                              |
| Storage 畸形          | 控制面只丢 Storage，保留其余指标和心跳                    |

回滚：`AGENT_HOST_STORAGE_PROBE=off`，或回退任一层代码。没有数据结构需要回滚。探针或验证故障不触发 drain、不删 retained sandbox、不改 Docker 配置、不阻断部署。

## 9. 自动化检查

| 层次     | 必须覆盖                                                                                                                                                |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 探针     | `/` 非 overlay、无 upperdir、mountinfo 缺失；`statvfs` 异常值；`off` 报 `disabled`；探针失败不影响 CPU、内存、inventory                                 |
| verdict  | 文件缺失、非法、超限、无本 upperdir 条目、条目为 `unsupported`／`unverified`、`size` 不等，各报对应原因；`verified` 且 `size` 相等才出数并带 `target`   |
| 验证脚本 | 通过；`df upperdir` 与挂载根 `size` 不等；配置含 `overlay2.size`；`findmnt` 失败；`used` 容差始终超出；`off` 跳过；任何失败都不进入致命 verdicts        |
| 计算     | 保留块、100%、0%、零分母、上限、半组字段、布尔值；当前值与 tooltip 同一公式                                                                             |
| 协议     | 新旧双向兼容；顶层 version 与 source 不变；未知 `source` 串在格式内被接受；Storage 畸形局部降级；有效值与 reason 互斥；8 KiB；旧快照读为 `not_reported` |
| 部署     | Compose 与 `.env` 渲染新变量与挂载；`create_host_path: false`                                                                                           |
| 前端     | 旧 API `undefined`；真正的 0% 与缺测；GiB／TiB 边界；表头与 9 列跨度；tooltip 含 target 与 source；`unverified` 文案                                    |

沿用 `tests/unit/test_host_capacity.py`、`tests/unit/test_capacity_protocol.py`、`tests/unit/test_retained_deploy.py`、`tests/unit/test_deploy_config.py`；前端扩展 capacity、format 和 page 测试。验证主机上的真实对照和反例 A 标记为 external，不能由 mock 替代。每个 PR 提交前跑 `make check`；前端 PR 跑 web 检查和构建。

Staging 验收：页面、admin API 和宿主 `df <挂载根>` 在同一时段口径一致；停止所有沙箱后 Storage 仍反映保留占用；`off` 后页面解释缺测，其余指标正常；删掉 verdict 文件后页面变为 `unverified`，`check` 后恢复；截图覆盖正常、缺测、未验证、过期、TiB 量级。

## 10. 后续

- 历史与趋势：磁盘按天填满，需要的是按小时聚合的长窗口，而不是现有 24 小时分钟表的扩展。届时再定义由部署注入的文件系统身份，用于断线。
- 归因：镜像、可写层、卷、构建缓存分项应走独立低频采集，有自己的观测时间。
- inode 与配额是另一类约束，独立口径。

## 附：修订记录

**相对 2026-09-15 初稿。** 初稿用只读空卷做探针，为证明卷与 overlay2 目录同盘，增加了宿主 `mountinfo` 挂载与解析器、每轮 Docker 元数据查询、设备号逐次校验、来源指纹、独立探针子进程、六列历史与 migration、第三张曲线和多来源界面。本稿改用 runner 根文件系统：内核保证它就是可写层所在的盘，同盘证明不再需要。初稿否定读 `/` 的理由是“无法证明统计范围”，这一点不成立：统计范围由 OverlayFS 的 statfs 语义定义。

**2026-09-16 第二次修订。** 第一版重写用宿主 `df upperdir` 对照容器读数并要求与 DockerRootDir 同设备号，评审指出三处错误：`df upperdir` 与容器一起落入同一 project quota，对照不能发现配额；同设备号要求会拒绝 containerd 分盘的正常配置，`target` 填 DockerRootDir 会指错盘；前后读数夹取假设占用单调。另外脚本报错拦不住已启动的采集器上报未验证数值。改为对照挂载根、`size` 严格相等、`used` 只做容差诊断、verdict 文件传递验证状态、失败不阻断部署。

**2026-09-16 实现记录。** 按本文实现，同一分支上分两个 commit：PR 1 = 探针→admin API→部署脚本→文档，PR 2 = Servers 的 Storage 列。实现时对本文没有写死的边角做了下列判定，均落在代码注释与测试里：

- 读不到 `/proc/self/mountinfo` 报 `source_unavailable`：第 4.3 节只规定“不是带 upperdir 的 overlay”是 `unsupported`，而“条件无法成立”与“条件不成立”是两回事。
- `AGENT_HOST_STORAGE_PROBE` 取未知名报 `unsupported` 并告警，绝不回退到根文件系统。
- verdict 文档里单条畸形条目被跳过（该 runner 因此报 `unverified`），不使整份文档作废；文档级错误（非 JSON、schema 版本不符、`entries` 非对象）仍整体作废。
- verdict 条目的 `fstype` 取宿主 `findmnt -T <upperdir>` 的结果（即第 4.3 节示例里的 `xfs`），不是容器内的 `overlay`。
- `used`／`avail` 容差复核最多三轮，轮间等 2 秒。
- `.storage-probe/` 是 `create_host_path: false` 的挂载源，`up` 无法准备目录或初始 verdict 时以明确报错停止，因为它同时是所有 runner 的启动前提；运行中的 `check` 写入失败只告警，不让可选存储验证阻断部署。
- 第 7 节“Storage 与 Memory 一样是瞬时观察”落在 Storage 的 tooltip 里；`CapacityPanel` 与 `CapacityMetric` 未改（面板里那句同类说明只服务于 CPU／内存历史图）。
- 表宽从 1080px 提到 1220px：九列在 1080px 下会把 Memory／Storage 的数值对挤成换行。
- 截图未做（实现环境无浏览器），Staging 验收中的截图项仍未完成。

**2026-09-16 评审后修订（PR #265）。** 评审提出三处，均已修，并各带一条会先失败的回归测试：

1. **部署验证会卡住部署。** storage 的 `docker exec describe`、`df`、`findmnt` 都没有超时，readiness deadline 也没传进来，一次卡住的调用就能让 `up` 永远不发布 ready 标记（评审用 `SHARED_READY_TIMEOUT=1` 复现）。现在：每次调用都由 coreutils `timeout` 限时（`STORAGE_CALL_TIMEOUT_S=8`，超时记 `unverified` 并继续）；整个验证另有预算（`STORAGE_ATTEST_BUDGET_S=30`），并以仍在前方的 readiness deadline 为上限；预算耗尽时未处理的副本不写条目（runner 侧即 `unverified`）。宿主若没有 `timeout`，整个验证直接跳过而不是让无界调用进入 rollout 路径。诚实边界写进了注释：处于不可中断 I/O 的进程任何预算都杀不掉。
   - 这是**部署脚本**的预算，第 6 节"不起子进程、不设单独预算"说的是 30 秒运行时采集周期，两者不冲突，运行时采样未改。
2. **Storage 局部降级不完整。** "有合法 `storage` 数值同时有 `missing.storage`"与"`missing.storage` 不在词表内"原先都会让**整份** CPU／内存／inventory 样本被丢弃。第 3.3 节的"只丢 Storage"现在覆盖全部 storage 形状的错误：这三类都在 `HostCapacityReport` 内降级为 `storage = null` + `missing.storage = "invalid_sample"`，其余字段与心跳不受影响。原来断言"整块丢弃"的新增测试已按设计改为断言局部降级。
3. **Tooltip 把保留块的含义写反了。** `used = total - free` **不**把保留块算作已用；仍空闲的保留块既不在 `used` 也不在 `available` 里，这正是二者相加不等于总量的原因。`used / (used + available)` 的分母口径不变，写反的说明（web tooltip、`admin.ts` 字段注释、`docs/operations.md`）已改正，并加了 `not.toContain('used counts the blocks')` 作为回归护栏。

仍待完成：第 5 节的真机验收实验（尤其反例 A）与 staging 截图。

**2026-09-16 第二轮 P1 修订。** 复核 `c11b5ac` 后确认 P2／P3 可关闭，P1 只修了一半：

- **预算只写在阶段边界上，每次调用仍固定 8 秒。** `SHARED_READY_TIMEOUT=2` + 探针不返回时，从首次 storage 调用到结束实测 8.08 秒——"最终会返回"做到了，"在剩余预算内返回"没有。现在每次调用重新计算 `min(STORAGE_CALL_TIMEOUT_S, 剩余预算 − 强制终止宽限期)`，负担不起就**不发起**该调用；容差重试之间的等待也计入同一预算。
- **`deadline <= SECONDS` 被当成了"手动 check"。** 它同样可能是**已耗尽预算的 rollout**，而原实现会给后者重新分配 30 秒。现在 `cmd_check` 把"这个 deadline 是不是 fence"显式传下去（`check_shared_live_stack` → `verify_storage_probe`）：有 fence 时预算就是 fence 剩余，已耗尽则一次调用都不做；只有无 fence 的手动 `check`（设计指定的 verdict 刷新路径）使用自身预算。
- **`timeout` 缺少强制终止阶段。** GNU `timeout` 默认只发 SIGTERM，对忽略该信号的进程会一直等下去；而测试替身直接 `kill -9`，比生产行为更强，恰好掩盖了这个缺口。现在使用 `timeout -k "$STORAGE_KILL_AFTER_S"`，宽限期计入预算（因此即使需要 KILL 的调用也在 deadline 内结束）；测试替身改为按 GNU 语义实现（预算处 TERM → 宽限后 KILL → 124／137，且**不传 `-k` 就不提供宽限**），并新增"探针忽略 SIGTERM 仍会被切断"的用例。
- **两条回归护栏都实测过：** 把按剩余预算改成固定 8 秒 → `test_a_partly_spent_fence_shrinks_the_call_bound` 报 "a call was handed the flat bound: [8]"；去掉 `-k` → 预算日志变成 `8 none`（断言失败），且忽略 SIGTERM 的用例会挂住直到测试被杀。

诚实边界收窄为一条：处于不可中断 I/O 的进程任何信号都杀不掉，`timeout` 自己会一直等它——这是本脚本无法设界的情形。其余（卡住的 `docker exec`、忽略 SIGTERM 的探针、仍响应信号的死挂载 `df`）都在界内。

**2026-09-16 第三轮修订（缺测分类）。** 复核 `57e76af` 后原 P1 关闭，另发现一处误判：`df <upperdir>` 被超时切断（或因预算耗尽被跳过）时返回空值，代码随即把这个空值与有效的挂载根 `size` 比较——二者"不相等"，于是记成 `unsupported` +"which is a project quota"。这是在**没有任何人建立该事实**的情况下声称宿主配置了配额，恰好违反本指标存在的理由。现在空值单独成一支：记 `unverified`，缺测原因写明 `df could not size ...`；只有两个**有效**数值不相等时才进入配额判定。测试用只在 upperdir 上挂起的 `df` 复现，并保留一条"真实配额仍记 `unsupported`"的用例，防止新分支吞掉对照本身要抓的情形。

**2026-09-16 真机验收。** 上述“实验和截图未完成”是各轮修订当时的状态。现已完成第 5 节所述 XFS 配额反例 A、vfs 反例 B、独立 loop-backed XFS 正例 C，并取得真实 staging 的 unverified 页面截图；结果见[验收记录](../acceptance/2026-09-16-admin-storage-capacity.md)。验收发现非 root 部署用户无法遍历 containerd 快照目录，正向容量展示因此未通过，合并仍需等待权限路径修复及 staging 复验。

**2026-09-16 验收问题修复。** 为非 root 部署用户增加上述一次性 metadata helper，并对实际 bind 的文件系统身份进行核对。原先直接读取宿主路径的分支保留；权限修复后的正向 staging 验收结果另行记录，不覆写首次验收失败的历史。

**2026-09-16 修复后复验。** `a2e2e36` 已完成实际部署、三个 runner 的 verified、页面和 HTTP 正向读数、已停止 retained sandbox 同盘确认、新 helper 下的 XFS A / C 以及清空 verdict 条目后的 `check` 恢复；详见[最新验收记录](../acceptance/2026-09-16-admin-storage-capacity.md)。权限阻塞关闭。staging 的 off、过期和宿主 unlink verdict 未在本轮强制构造，不把原地清空文件内容等同于删除 bind source。

**2026-09-16 verdict 删除修复。** 补验确认单文件 bind 在宿主 unlink 后仍持有旧 inode，重建文件也不会被运行中 runner 看到。因此改为只读挂载专用 `.storage-probe/` 目录，每轮打开其中的 `verdict.json`，并保持目录本身不变。新增真实 Docker external 回归，直接使用 Compose 挂载配置验证同一运行中容器能观察删除、重建及 rename 替换；恢复单文件挂载时该回归在删除后的 unverified 断言处失败。目录权限、初始化、`check` 重建与写入失败隔离亦有单测；未改采样口径或 quota attestation。

**2026-09-17 独立评审后的修订。** 一轮多视角评审在部署脚本这一段发现十余处问题，逐条核实后修复如下，均带回归测试：

- **验证抢占就绪预算。** 存储验证原本排在三个致命探测之前、共用同一条 fence。它本身不致命，但花掉的秒数正是那些探测用来重试的；staging 的 120 秒 fence 下，控制面拨号可能只剩一次机会，失败即回滚整个部署。现改为在全部致命检查之后运行，并让第一次占用比较复用并行阶段已经取到的 describe 文档，不再为两个数字多跑一次 `docker exec`。
- **helper 挂载参数要求 Docker 25。** `bind-recursive=disabled` 由 CLI 解析，v25 才引入；而 helper 恰是非 root 部署用户的正常路径，低于 25 的主机会永远 `unverified`。改用语义相同、19.03 起即有的 `bind-nonrecursive=true`。
- **helper 失败被记成 findmnt 失败**，且它自己的 stderr 写进一个无人读取、随后即删的文件。现在按退出码分别归因，并把 helper 的最后几行原样打印。
- **`AGENT_HOST_STORAGE_PROBE` 两侧词表不一致。** 脚本只认小写 `off`，runner 还认 `OFF/0/false/no/disabled`，于是同一份配置能让日志写“storage verified”而页面显示“已关闭”。现在只接受 `overlay_root` 和 `off`，在 `validate_retained_config` 里当场拒绝其它拼写；`off` 时 `check` 会清空 verdict，使切换前启动的副本立即停止发布容量（真正停止测量仍需 `up`）。
- **转义路径无法验证。** runner 按设计保留内核的 `\040` 等转义（那是 verdict 的键），脚本却把这串东西直接交给 `findmnt`、`df` 和 `docker --mount`。现在只在交给宿主工具前反转义，键本身不变。
- **挂载计划在同目标覆盖挂载时选错。** `max()` 取首个最长匹配，而 findmnt 按创建顺序输出，于是计划描述的是被遮住的那个挂载，helper 的身份核对必然失败。改为取最后一个。
- **verdict 原地截断写入。** 目录挂载之后已无需保留 inode，改为同目录写临时文件再 rename，避免采样撞上写入窗口读到空文件。两个写入者合并为一个函数。
- **verdict 里的文件系统类型。** 占用不一致分支和 verified 日志写的是容器内的 `overlay`；两次独立计时的 `findmnt` 中第二次失败时还会静默回退成它。现在一次调用取 `FSTYPE,TARGET`，取不到就记 `null`。
- **helper 以 uid 0 运行**并挂载宿主挂载根（默认拓扑下即 `/`）。它只做 `O_PATH` 打开加 `fstatvfs`，两者都不需要权限，改为 `--user 65534:65534`。
- **控制面的局部降级越界。** `missing` 缺失或非对象时会被伪造成空表，导致一份结构非法的心跳能否被接受取决于 storage 是否同时畸形。现在这种输入原样返回，由必填字段照常丢弃。
- **页面数字两套取整规则。** Storage 与 Memory 并排却分别打印 `1.5 / 2.0 TiB` 和 `4 / 8 GiB`，且 1.2499 TiB 被显示成 1.3。两者现在共用一个函数，Memory 的显示不变。
- **`describe` 与运行时探针读 mountinfo 的方式不同**（无上限、有损解码对有上限、严格解码），可能出现部署写下 verified 而心跳始终 `source_unavailable`。现在共用一个读取函数；模块中段的 `__main__` 块相应移到文件末尾，否则 `python -m` 会在定义之前停下。

评审驳回两条候选，不作修改：`ps | grep -q` 在 `pipefail` 下的 SIGPIPE 误判（procps 的 `ps` 自带处理函数，以 0 退出）、以及浮点百分比向上取整多一个点（字节数都是块大小的整数倍，2^53 以内乘 100 精确）。

**这些改动尚未在 staging 复验。** 上一轮验收记录的是 `5d0050f`；其中部署路径的部分需要在合并前按第 5 节和第 10.3 节重跑，尤其是 helper 以 `nobody` 运行、`bind-nonrecursive` 和新的调用顺序。

**2026-09-16 unlink 真机复验通过（`5d0050f`）。** 实际 staging 的三个 runner 在宿主文件被真正删除后全部报告 unverified；新心跳和页面正确局部降级。`check` 重建 verdict 后，API 和页面恢复容量，三个容器的 ID／StartedAt 及挂载目录 inode 全程未变。此项不再是未测项；[最新验收记录](../acceptance/2026-09-16-admin-storage-capacity.md) 包含前后截图、时间和字段。off、stale 页面仍未在 staging 强制构造，沿用自动化覆盖。
