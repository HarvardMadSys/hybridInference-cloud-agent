# PR #265 存储容量真机验收

## 结论

**验收未通过，暂不合并。** `overlay_root` 的配额反例、非 overlay 反例及独立文件系统正例均已在真实 Linux 内核和 Docker 上跑通；但 staging 的实际部署用户无法遍历 containerd 的快照目录，三个 runner 的部署验证都得到 `unverified`，页面始终显示 **Storage not verified**。正常容量从 staging runner 到页面的验收仍待修复后完成。

- 被测提交：`f425811ac975ce4e51e845ecefefa7baf8c454d6`，PR #265；本记录不把后续文档提交算作被测代码。
- 日期：2026-09-16，以下时间均为 UTC。
- 实际 PR 部署：[首次运行](https://github.com/HarvardMadSys/hybridInference-cloud-agent/actions/runs/35086167257)、[截图验收运行](https://github.com/HarvardMadSys/hybridInference-cloud-agent/actions/runs/35086802850)，两端部署均成功。
- 首次部署之后，dev 的一次自动部署将 staging 更新到了 `6669a88308e19fd655899dbf7fe5c67846325326`。再次部署 PR 取得页面、心跳、权限证据后，已[恢复最新 dev](https://github.com/HarvardMadSys/hybridInference-cloud-agent/actions/runs/35087238607)：两项部署任务成功，控制面和 runner 的 `.deployed-sha` 均确认为 `6669a88308e19fd655899dbf7fe5c67846325326`。

## 1. 真实 staging：发现部署权限缺口

环境：控制面和 web 在 spark2，runner 在 rtx6000b；Docker 29.2.1，`overlayfs` driver，containerd image store，宿主 ext4，Linux `6.17.0-22-generic`。实际部署由普通用户 murphy 执行，具有 Docker socket 访问权，不能无密码 sudo。

复现（在部署宿主上，以部署用户运行）：

```bash
runner=freeinference-cloud-agent-sandbox-staging-kata-runner-1
stat -c '%A %U %G %n' /var/lib/containerd
upper=$(docker exec "$runner" python -m cloud_agent_host.host_capacity describe |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["upperdir"])')
findmnt -n -o TARGET,FSTYPE,SOURCE -T "$upper"
df -B1 --output=size,used,avail,target "$upper"
```

实际结果：

```text
drwx------ root root /var/lib/containerd
upperdir=/var/lib/containerd/io.containerd.snapshotter.v1.overlayfs/snapshots/39577/fs
findmnt_exit=1
df_exit=1
df: .../39577/fs: Permission denied
```

`/var/lib/containerd` 本身所在文件系统的总量是 `1966736678912 B`。runner 的描述命令也能读到这个总量，但部署侧不能证明 upperdir 所在挂载，因而不能签发 `verified`。首次部署日志中三个副本均出现 `findmnt could not locate the filesystem containing ...`，verdict 的 `mount_root` 为 null，状态为 `unverified`。

10:49:23 的控制面数据库快照：

```json
{
  "host": "rtx6000b-kata",
  "status": "online",
  "slots_used": 0,
  "slots_total": 3,
  "storage": null,
  "missing": { "cpu": null, "memory": null, "storage": "unverified" },
  "cpu_percent": 8.606815989152825,
  "memory_total_bytes": 540299722752,
  "memory_available_bytes": 495542038528,
  "inventory": { "running": 0, "stopped": 25 }
}
```

上面是相关字段的摘录，不是 HTTP 响应原文。浏览器实际页面与它一致：Storage not verified，CPU、内存、在线状态与 retained inventory 正常；展开历史曲线也正常。部署不被可选存储验证阻断、缺测不显示为 0% 的降级路径通过。

![staging：Storage not verified，其他指标仍正常](assets/2026-09-16-pr265-storage-unverified.png)

**合并前需要修复：** 为部署 attestation 提供能够读取目标挂载和 upperdir 元数据的受控执行路径，并以现有非 root 部署用户重新验收。不应为了让实验通过而临时放宽 `/var/lib/containerd` 权限；本次没有修改该目录权限、没有伪造 verdict。

## 2. 隔离实验：机制验证通过

在 rtx6000b 上创建临时 `docker:29.2.1-dind` 容器，独立网络与挂载命名空间，不挂宿主 Docker socket、宿主 data-root 或业务凭据。容器内创建 32 GiB 稀疏文件和 loop 设备，格式化为 XFS，以 `prjquota` 挂载 `/lab/disk`。嵌套 daemon 使用 classic `overlay2`，data-root 为 `/lab/disk/docker`。另一个嵌套 daemon 使用 `vfs`，data-root 为 `/lab/disk/vfs`。

测试镜像直接来自本次 staging 构建：

```text
OCI manifest: 6620051cedfa3cfe8ff2e2c101f029a773622a59a176f740bd42a923fa25b3aa
image config: b71fefbd17b1e7946100a23474f3b7598364fce7b6ab57aea115593e61a36140
host_capacity.py SHA-256:
d724c54fe4fe56ddc1450e24421b5a452a501027ee5e8ff118e4337379c8d24c
```

镜像中的探针文件与被测提交的文件哈希相同。部署验证使用从该提交 `sandbox_host.sh` 原样提取的存储常量和函数，通过小型 harness 提供日志函数、环境变量和 verdict 输出目录；调用真实的 Docker、GNU df、findmnt、timeout，没有替换读数。这个 harness 验证 attestation 算法，不能替代第 1 节的实际部署权限验收。

### A：10 GiB project quota

同一 runner 镜像以 `--storage-opt size=10G` 启动，网络为 none。

| 读取位置      |   total (B) |   used (B) | available (B) |
| ------------- | ----------: | ---------: | ------------: |
| runner `/`    | 10737418240 |    2068480 |   10735349760 |
| `df upperdir` | 10737418240 |    2068480 |   10735349760 |
| XFS 挂载根    | 33939488768 | 1089085440 |   32850403328 |

结果：真实复现“容器和 upperdir 一起返回配额”的情形。attestation 以 upperdir 与挂载根总量不相等判为 `unsupported`，reason 明确为 project quota；将真实 verdict 交给 runner 探针后，`sample()` 返回 `{"reason":"unsupported"}`，不发布 10 GiB 为磁盘容量。

本次验证的是 **XFS project quota**。Docker 的 `overlay2.size` 支持条件是 XFS 加 pquota；没有以 ext4 重复此反例，也不据此声称 Docker 支持 ext4 的此配置。

### B：非 overlay 根

同一镜像在 `vfs` daemon 下启动，根文件系统显示为 XFS。`describe` 返回 `ok=false, reason=unsupported, upperdir=null`；attestation 不写 verified 条目；运行时 `sample()` 返回 `{"reason":"unsupported"}`。通过。

### C：独立文件系统，无配额

| 读取位置       |     total (B) |     used (B) | available (B) |
| -------------- | ------------: | -----------: | ------------: |
| runner `/`     |   33939488768 |   1086836736 |   32852652032 |
| `df upperdir`  |   33939488768 |   1086836736 |   32852652032 |
| XFS 挂载根     |   33939488768 |   1086836736 |   32852652032 |
| 外层根文件系统 | 1966736678912 | 683867594752 | 1182888833024 |

三个数逐项相等。`findmnt` 选中 Docker 创建的 data-root 自绑定挂载 `/lab/disk/docker`（来源 `/dev/loop1[/docker]`），attestation 为 `verified`，探针实际返回 `target=/lab/disk/docker`、`total_bytes=33939488768`。

此正例证明探针能定位**独立 loop-backed 文件系统**，没有误读外层根文件系统。它不是第二块物理磁盘的实测，也不是 containerd snapshotter 独立磁盘的实测。

## 3. 验收边界与后续

- A、B、C 已有真实内核、文件系统和 Docker 证据，不再只是 mock 测试。
- staging 降级路径通过；实际正向容量展示失败，不能把整体验收标为完成。
- staging 的 retained sandbox 与 runner 的 upperdir 同挂载确认，以及正向 Storage 数字和 tooltip 的页面验收，需在权限修复后补齐。本次没有启动或修改用户的 retained sandbox。
- 设计第 9 节的其余 staging 场景（off、verdict 删除后恢复、过期、TiB 数字展示）未强制构造；本次页面证据覆盖真实 `unverified` 状态及其他指标继续更新。
- 临时内层容器、两个 daemon、XFS 挂载、loop 设备和外层实验容器已清理。未修改生产栈或业务 daemon 配置。
- 原始实验输出、执行脚本、两次 PR 部署日志、数据库摘录和截图保存在执行机器的 `pr265-storage-acceptance-20260916` 证据目录；本文件记录关键数值及其限制。

## 4. 仓库检查

- `env -u CODEX_VERSION make check`：4039 passed、489 deselected；format、lint、typecheck 通过。首次直接运行受到工具进程注入的 `CODEX_VERSION` 干扰，部署测试提前拒绝这个与仓库固定版本不同的值；仅在重跑进程中去掉该变量后通过，未修改测试或断言。
- 两份 Markdown 的 Prettier 检查和 `git diff --check` 通过。
- 此次回填只包含验收记录、截图和设计文档更新，不包含权限修复；自动化检查通过不能替代上述未通过的 staging 正向验收。
