# 工作区文件打开 baseline profile

日期：2026-09-14。状态：**已实测的 baseline，尚未实施任何优化**。
本文只是把"打开一个文件要多久、时间花在哪里"变成可复现的数字，供后续优化做前后对比；
它不主张已发现线上瓶颈，也不构成 SLO。

代码版本：`01cfbae`（分支 `codex/fix/follow-up-continuity`）。
相关设计：[沙盒启动测量设计](2026-09-14-sandbox-startup-measurement-design.zh-CN.md)、
[平台操作测量清单](2026-09-14-platform-operation-measurement-inventory.zh-CN.md)。

## 1. 被测路径

用户点开一个文件时，实际执行的是：

```text
浏览器 → CP GET /v1/agent/jobs/{job}/files
       → owner.operation_opened()            # 可能唤醒已停止的容器
       → broker GET /workspaces/{id}/files
       → require_network_phase(spec, "agent") # 每次重新校验 Docker 状态
       → read_retained_file()                 # docker exec + 新起 python3
```

`cloud_agent/api/routes.py:2838`、`cloud_agent_host/workspace_broker.py:1329`、
`cloud_agent_host/chat_sandbox.py:1716`、`cloud_agent_host/retained_workspace.py:260`。

## 2. 测量方法与环境

本文数字来自**外部 benchmark harness**（清单 §0 "自身开销也要测" 的第 3 类证据）：
它从进程外调用平台代码并自行计时，不依赖产品内的 profiling 埋点，因此与
"平台 profiling 默认关闭" 的决定不冲突；开关关闭时这份 baseline 仍可复现。

`scripts/bench_workspace_file_open.py --mode docker` 用平台自己的 `ChatSandboxManager`
建真实容器（两个 backplane 网络 + proxy/gateway/MCP relay + 每 chat 的 internal bridge），
然后按上面两步计时；`_census()` 挂钩 `chat_sandbox.subprocess.run` 数每一次 docker CLI 调用。

| 项 | 值 |
| --- | --- |
| 宿主机 | Apple M4 Pro（14 核），macOS 15.7.7 |
| 容器运行时 | colima/Lima VM，4 vCPU / 6 GiB，kernel 6.8.0-64-generic，Docker 28.4.0，**runc** |
| 镜像 | `debian:bookworm-slim` + `python3`（3.11，路径 `/usr/bin/python3`，与沙盒一致） |
| 空闲停止 | `AGENT_CHAT_IDLE_TIMEOUT_S` 默认 1800 s（`ENVIRONMENT.md:183`） |

**这不是生产数字**：生产是 Linux ARM64 原生 daemon + **Kata**（VM 隔离），exec 要穿 VM，
且 `_sync_kata_service_hosts()`（`chat_sandbox.py:591`）会再加一次特权 exec。
本机 VM 内没有 `kata-runtime`，所以 Kata 项只能上真 host 测。
数字的意义在**结构**与**相对占比**，不在绝对值。

## 3. 暖路径（容器运行中），n=12

| 场景 | p50 | p95 | 描述 |
| --- | --- | --- | --- |
| 目录 40 项 | 160.4 | 170.1 | |
| 4 KiB | 164.9 | 167.7 | |
| 64 KiB | 172.3 | 203.6 | |
| 512 KiB | 173.2 | 185.5 | 预览上限 |

拆成 broker 的两步（p50）：`require_network_phase` **107–116 ms**，`read_retained_file`
（docker exec + helper）**53–59 ms**。前面再套一层 broker 形状的 HTTP 路由只加 ~10 ms。
另一轮 20 次采样得到 178–183 ms p50，运行间波动约 ±10%。

**约 2/3 的时间花在校验 Docker 状态，而不是读文件。**

### 一次打开的 docker CLI 调用清单（11 次）

```text
 1. network inspect <本 chat agent 网桥>     _ensure_phase_network
 2. network inspect <agent backplane>        _service_owners
 3. container inspect <两个 relay id>        backplane peers
 4. container inspect <relay 1>              _ensure_service_connection
 5. container inspect <relay 2>              同上
 6. network inspect <本 chat agent 网桥>      _reconcile_phase_members
 7. container inspect <chat 容器>            同上
 8. network inspect <本 chat agent 网桥>      同上（refresh）
 9. container inspect <chat 容器>            require_network_phase 的 _require
10. container inspect <chat 容器>            exec_capture 自己的 _require
11. exec --user 10001:10001 python3 -I -c <helper> /workspace <path> 524288 5000
```

即 **10 次 inspect + 1 次 exec**；真正读文件只有第 11 条。原语成本实测：
`docker version` 15.1 ms、`container inspect` 12.6 ms、`network inspect` 12.7 ms——
10 × 12.6 ≈ 126 ms 对上 verify 的 ~110 ms；exec ~45 ms + helper ~15 ms 对上 read 的 ~55 ms。
账是闭合的。

## 4. 冷路径（空闲停止之后），n=6

容器被空闲停止后，下一次打开要先唤醒（`operation_opened()` → `_ensure_running()` →
`manager.start(spec, network_phase="agent")`，`shared_session_supervisor.py:2145`）：

| 步骤 | p50 | p95 | min | max |
| --- | --- | --- | --- | --- |
| wake | 495.5 | 571.5 | 491.7 | 571.5 |
| verify | 115.1 | 116.8 | 112.5 | 116.8 |
| read | 58.0 | 78.7 | 55.6 | 78.7 |
| **合计** | **670.7** | **742.9** | **659.7** | **742.9** |

一次冷打开共 **52 次 docker CLI 调用**（含 stop）：26× container inspect、16× network inspect、
3× network connect、2× network disconnect、1× network create、1× network rm、1× stop、1× start、1× exec。

按默认 30 分钟空闲时钟，任何隔了半小时回来看文件的用户都落在这一档：**670 ms 里 wake 占 74%**，
而 `docker start` 只是 52 次调用中的 1 次。唤醒的真实成本几乎全在重复的网络/容器校验与重建。

### 单次冷打开的逐调用时间线（一次采样，不含 stop）

```text
wake   31 calls  466 ms   21× container inspect / 11× network inspect / 3× network connect
                          / 1× network create / 1× docker start (69 ms)
                          其中 docker start 69 ms、network create 19 ms、3× network connect ≈ 61 ms
verify  9 calls  107 ms   5× container inspect + 4× network inspect，全部是校验
read    2 calls   55 ms   1× container inspect (11.6) + 1× exec python3 (43.7)
合计   42 calls  637 ms
```

把"真活"（`docker start` + 网络创建/连接 + exec，共 ~174 ms）与其余部分分开看：
**约 72% 的冷打开时间花在 docker CLI 进程启动与重复校验上**，42 次调用里有 36 次是 inspect。
暖路径是同一形状的小号版本：11 次调用里 10 次 inspect，真实读取只有 1 次 exec。

## 5. 并发（同一 4 vCPU VM，每档 5 轮）

| 并发打开数 | 单次 p50 | 整批 wall p50 |
| --- | --- | --- |
| 1 | 173.9 | 174.1 |
| 2 | 173.2 | 173.3 |
| 4 | 205.5 | 210.4 |
| 8 | 256.5 | 298.4 |

2 路无损耗，4 路起劣化：每次打开要 fork 11 个 docker 进程，CPU 竞争明显。

## 6. 覆盖范围与未测项

已测：broker 侧暖/冷两步耗时、docker CLI 调用次数、原语成本、并发劣化。
未测：Kata 运行时（VM 穿越 + guest `/etc/hosts` 特权 exec）；CP 侧（`_owned_job`、
`broker_for_job` 两次 DB 查询；未配 `AGENT_REPO_ALLOWLIST` 时每次打开还会去 GitHub 列
installation repositories，`github_app.py:405`）；共享 chat 每次打开的第二次 CP→broker 请求
（`POST /shared-sandboxes/{id}/register`）；CP↔host 真实 RTT；浏览器渲染。

## 7. 优化候选（按证据排序，**尚未实施、收益未验证**）

1. **合并一次操作内的重复校验**：11 次调用里 9 次是同一次打开的重复确认（清单 3–10）。
   按原语成本估算可省 ~100 ms 中的大部分。
2. **改走 Docker API（socket）而不是 fork CLI**：单次调用 ~12.6 ms 的进程启动可省，
   并发档位的 CPU 竞争也随之下降。
3. **冷路径的唤醒**：wake 495 ms / 52 次调用是最大单项；`_release_sandbox_networks` 与
   唤醒时的重建/校验链条值得单独 profile 一次再动。
4. **前端请求粒度**：目录展开与文件点击各自一次请求，无批量 stat、无短 TTL 缓存
   （`web/src/components/features/agents/useAgentJobs.ts:620`）。

任何一条改完都用同一脚本、同一模式、同环境复测；模拟栈数字不得当作线上数据。

## 8. 复现

```bash
python scripts/bench_workspace_file_open.py --mode docker --build-image --iterations 20 --http
python scripts/bench_workspace_file_open.py --mode docker --cold 6        # 冷路径
python scripts/bench_workspace_file_open.py --mode live --url … --token … --job-id …   # 线上
```

Docker 模式需要可达的 daemon（本次用 `colima start`），跑完会自动清理容器与网络；
bench 镜像保留在本地，`docker rmi cae-bench-guest:bookworm` 可删除。
