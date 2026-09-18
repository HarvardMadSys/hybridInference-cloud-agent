# 任务输入框的加号菜单

把 Skills 从控制行里挪进一个加号菜单，并在同一个菜单里加上文件上传。本文定义划分
原则、交互细节、上传的完整投递路径，以及明确不做的几件事。

## 1. 问题

新任务输入框（`web/src/components/features/agents/TaskComposer.tsx`）底部一行现在
从左到右摆着六个控件：仓库、分支、分隔线、Agent、模型、推理强度、Skills，最右是
运行按钮。容器是 `flex-wrap`，六个已经会换行。

但拥挤只是表面。真正的问题是这一行把两类东西摆成了同一种控件：

- 仓库 / 分支 / Agent / 模型 / 推理强度 回答的是「这次任务在哪里、怎么跑」。
  每项恰好一个值，永远有意义，所以永远可见。
- Skills 回答的是「给它带上什么」。零个或多个，多数任务一个都不带。

把后者做成和前者一样的行内控件，等于说它们是同一类选择。上传进来之后这件事会更
明显：没有人会觉得「我传的两个截图」和「模型是哪个」是一回事。

## 2. 划分原则

> **一行里留「怎么跑」，加号里放「带什么」。**

- **留在行内**：仓库、分支、Agent、模型、推理强度。单值、必选、总是可见。
- **进加号**：上传文件、上传文件夹、Skills。零到多个，按需添加。

这条规则同时回答了「Skills 为什么该挪」和「以后什么能进加号」，不必每加一项重新
讨论一次。

## 3. 菜单内容

点加号弹出一个轻量浮层（popover，不是模态），三项：

```
┌──────────────────────────────────┐
│  📄  上传文件                     │
│  📁  上传文件夹                   │
├──────────────────────────────────┤
│  ⚡  Skills                  3 ›  │
└──────────────────────────────────┘
```

**没有上传弹窗。** 两个上传项各自直接触发一个 `<input type="file">`，系统自己的
文件选择器就是选择界面，用户已经会用了。之所以是两项而不是一项：网页里选文件和选
文件夹是两个不同的 input，后者要带 `webkitdirectory`，一个 input 做不了两件事。
拖放到输入框上则一次就能两者都收。

Skills 仍然需要自己的弹窗 —— 它是一个从库里挑选的列表，不是文件系统里的东西。

## 4. 交互细节

### 4.1 加号的位置

放在控制行最左，在仓库选择器之前。它是一个**动作**（"给这条消息加点东西"），不是
一个设置；所有人熟悉的聊天输入框都把这个动作放在左端。运行按钮继续独占右端，这
一行原本的左右不对称保持不变。

### 4.2 附件显示在正文里，不在控件行

这是两类东西的另一个分界：

- **上传的文件**是这条消息的**内容**。它显示在输入框正文里，文本框和控件行之间的
  一条附件带上，每个文件一枚可删的 chip。要看得见传了什么，也要能一个一个删掉。
- **Skills** 是一个**设定**。它在控件行里不显示任何东西，数量只在菜单里看
  （`Skills 3 ›`）。

Skills 不给 chip 是有意的：输入框一打开就带着账户默认值（`default_enabled`），
所以那枚 chip 几乎永远在。一个永远在的 chip 什么都没省下 —— 只是把 Skills 按钮
换了个样子，还多了一个加号。

代价要说清楚：**今天在行里能一眼看到的 `Skills · N`，之后要点一下加号才看得到。**
这是为了让控件行真的变干净而付的价，不是疏忽。

### 4.3 键盘与可达性

- 加号按钮：`aria-haspopup="menu"`、`aria-expanded`。
- 浮层：`role="menu"` / `role="menuitem"`，上下键移动，Enter 打开，Esc 关闭并把
  焦点还给加号。
- Skills 弹窗复用 `SkillDialog` —— 它已经做了焦点陷阱、Esc 关闭和 portal。
- 上传项是真正的 `<input type="file">`（视觉上隐藏、由菜单项触发），所以键盘和
  辅助技术拿到的是原生控件。拖放只是额外通道，不是唯一通道。

### 4.4 文案与 i18n

现在 `SkillPicker` 里的字符串是硬编码英文（"Skills"、"For this conversation"、
"Apply"…），而 `TaskComposer` 走 `useT`，同一行里两套做法。新菜单一律走 `useT`，
并把 `SkillPicker` 的既有文案一并补进 `en.ts` 和 `zh-CN.ts`，键名用
`composer.attach.*`。`i18n.test.ts` 会检查两份字典是否对齐。

## 5. 上传的设计

### 5.1 目录就是一组带相对路径的文件

`<input type="file" webkitdirectory>` 拿到的是一组带 `webkitRelativePath` 的文件，
所以「上传目录」不需要任何目录概念，只要保留每个文件的相对路径。拖放整个文件夹走
`DataTransferItem.webkitGetAsEntry()`，结果一样。

每条相对路径按和 `_repository_content_endpoint`（`cloud_agent/github_app.py:611`）
同一套规则校验：拒绝 `..`、空段、`.`、`.git`、反斜杠、NUL 和盘符前缀。

### 5.2 存在哪里：Postgres 的 BYTEA，不引入对象存储

这个服务今天**没有**对象存储 —— `boto3` / `minio` / S3 客户端在 `cloud_agent`、
`cloud_agent_host` 和 `pyproject.toml` 里都搜不到。为这个功能引入一套，意味着新的
基础设施、新的凭据和新的部署面。

限额足够小就不必这么做。新表一张，内容存 `BYTEA` 并用 CHECK 卡死上限，和
`0013_personal_skills.py` 里 `instructions TEXT CHECK (octet_length(...) BETWEEN 1 AND 65536)`
是同一种做法：

```sql
CREATE TABLE agent_job_attachments (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    job_id TEXT,                         -- NULL = 已上传但还没被任务引用
    path TEXT NOT NULL CHECK (char_length(path) BETWEEN 1 AND 512),
    content BYTEA NOT NULL CHECK (octet_length(content) BETWEEN 1 AND 5242880),
    content_type TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

限额（可以后调，先定死）：

| 项 | 上限 |
| --- | --- |
| 单文件 | 5 MB |
| 单次任务合计 | 20 MB |
| 文件数 | 50 |

超限的错误标在具体那个文件的 chip 上，其余照常附上，并且发生在按运行**之前**。

### 5.3 先上传，再创建任务

两种做法里选前者：

1. **暂存上传** —— `POST /v1/agent/uploads`（multipart）逐个返回 id，创建任务时
   在 JSON 里带 `attachment_ids`。
2. **创建时一起传** —— 把 `POST /v1/agent/jobs` 改成 multipart。

选 1。它让 `POST /v1/agent/jobs` 保持 JSON，`Idempotency-Key` 的提交指纹形状不变；
而且用户还在打字的时候文件就已经在传了。没有被任何任务引用的上传
（`job_id IS NULL`）过 24 小时清掉。

### 5.4 为什么这件事必须平台来做

五个 CLI 适配器里**没有任何 upload 概念** —— `cloud_agent_host/runtimes.py` 里
搜不到 attach / upload / image 之类的东西。它们只认两样：提示词，和一个工作目录。

所以没有「harness 自带的上传功能」可以接。文件必须在 Agent 起来**之前**就已经躺在
磁盘上，Agent 看到的只是一个普通路径。这既是约束，也是好消息：不需要按 runtime 做
五套适配，一套投递路径对五个 CLI 都成立。

### 5.5 字节怎么进沙箱

`cloud_agent_host/source_archive.py` 的模块注释写得很清楚：**Docker 的 archive API
是进入 `/workspace` 的唯一边界**。仓库 checkout 走的就是这条路
（`runner.py:1321`：`build_source_archive` 打 tar，`manager.import_archive` 导入）。
上传沿同一条路进去：

1. Runner 拿 worker token 沿已有的 worker 通道取回字节
   （新增 `GET /v1/agent/worker/jobs/{job_id}/attachments`）。
2. 打成规范化 tar，uid/gid 归一到 `manager.backend.uid`，权限位统一成 `0644`
   （不给执行位），路径再校验一遍。
3. `import_archive` 到 `/uploads`。

**不新增任何网络出口。** 没有新的网络目的地、没有新的凭据、没有新的共享工作区文件。
Agent 阶段的出口仍然是 `platform_only`：字节在 claim 阶段就已经在容器里了，沙箱
自己一次都不用往外连。

### 5.6 为什么不能放在 /workspace

`CHAT_WORKDIR` 是 `/workspace`（`cloud_agent_host/chat_sandbox.py:100`），而工作区
的发布路径跑的是 `git add -A -N`（`cloud_agent_host/workspace_broker.py:1130`）。
**落在 `/workspace` 里的上传会被收进 diff，然后出现在 draft PR 里。**

所以目标目录是工作区外面的 `/uploads`，并且对沙箱只读。只读还有一个好处：Agent
不会「就地改一改」上传的文件，要改就得先复制出来，改动因此永远是显式的。

### 5.7 Agent 怎么知道文件在哪

claim 时把路径清单前置到任务提示词，和 Skills 的指令快照用同一个通道
（见 `docs/specs/2026-09-17-personal-skills.md`）：

```
The user attached these files to this task, under /uploads (read-only):
- /uploads/composer-mock.png
- /uploads/staging-2026-09-17.log
- /uploads/specs/api-v2.md
- /uploads/specs/diagrams/flow.svg
```

追加轮次用同一个字段，文件累加进同一个目录；容器是保留的，`/uploads` 自然还在。
每个被接受的轮次保存自己的附件清单，Fork 和重试拿到的是同一批内容 —— 和
`agent_jobs.skills` 的快照语义一致。

### 5.8 安全

- **上传是唯一能把凭据放进沙箱的口子。**「沙箱里不含任何凭据」是这个产品写在输入
  框副标题里的承诺，而上传绕过了它。第一次附上文件时，附件带下面给一句明确提示，
  不是脚注。
- **不自动解压。** `.zip` / `.tar.gz` 原样存、原样投递，没有 zip bomb 面。
- **不给执行位。** 全部 `0644`。
- **内容本身是不可信输入**，但这不是新的信任边界 —— 沙箱里跑的仓库代码本来就
  不可信。值得写下来，不值得为它加新机制。
- **配额按用户算**，防止一个人把控制面的数据库撑爆。

### 5.9 顺带打开的一扇门：Skills 也可以走这条路

今天的 Skill **不落到磁盘上**。`resolve_skills` 把选中的内容做成快照存进
`agent_jobs.skills`，worker claim 时 `prompt_with_skills`
（`cloud_agent/skills/selection.py`）把整份快照 JSON 序列化，拼在任务提示词前面，
调用点在 `cloud_agent/api/routes.py:3916`。`ClaimedJob` 里没有 skills 字段 ——
宿主机从头到尾不知道有这回事，五个 CLI 走的都是各自的普通提示词通道。

这套做法的代价是固定的：最多 16 个 skill、合计 128 KB 指令
（`cloud_agent/skills/models.py`），**每一轮都要付这份 token**，没有渐进披露；
而且只带指令，skill 里引用的脚本和资源一概不装。

上传的投递路径一旦通了，这里就多了一个选项：tar 打包经 Docker archive API 送进
工作区外的一个目录，这套机制对 skill 目录同样成立。那样 claude-code 可以用它原生
的 skill 发现和渐进披露，不必每轮把全文塞进上下文；其余四个 CLI 退回现在的 prompt
方式即可。**本文不做这件事**，记在这里是因为 P2 顺带把它的成本降到了很低。

## 6. 后续对话框（SideChat）

追加轮次的输入框今天只有模型和推理强度两个选择器，没有 Skills。同一个加号应当
出现在那里，三项都有。这一步排在主输入框之后，不和第一期绑在一起。

## 7. 明确不做的四件事

写下来是为了以后不用再讨论一遍。

**上传弹窗。** 系统自己的文件选择器就是选择界面。中间再套一层自己画的弹窗，只是
多一次点击和一套要维护的拖放代码。

**挑仓库里已有的文件和目录。** 沙箱本来就把整个仓库 clone 进去了，文件早就在，
Agent 自己能找。这一项的价值只是「指路」，省几轮 grep，而代价是一个新字段加一个
仓库目录树接口。真需要的时候，在提示词里写路径也能达到八成效果。

**准备脚本。** `setup_script` 字段是通的（`cloud_agent/schemas.py:108`），加个文本
框就能发。但 issue #230 指出缓存键包含脚本哈希，**按任务临时写的脚本永远不会命中
缓存**，而「每个仓库一份稳定脚本」才是这个缓存开始回本的前提。等 #230 决定脚本存
在哪里。

**工具（MCP）。** `mcp_bindings` 也是通的，issue #212 也确实需要界面来解释哪些
runtime 不能接 MCP。但那是 MCP 自己的问题，不是加号的问题，而且菜单一到四五项就
从入口变成了抽屉。分开做。

## 8. 分期

| 阶段 | 内容 | 后端 |
| --- | --- | --- |
| **P1** | 加号按钮、菜单浮层、Skills 搬进去、控件行里不再有 Skills、i18n 键补齐 | 无 |
| **P2** | 上传：新表、`POST /v1/agent/uploads`、`attachment_ids`、附件带、worker 取回、tar 导入 `/uploads`、claim 期提示词 | 一张表、两个接口、runner 一段 |
| **P3** | SideChat 的加号 | 无 |

P1 独立可发：它不改任何提交内容，只改这些控件长在哪里。

## 9. 验收

- 控制行在 1280px 宽度下不再换行，行里只多了一个加号。
- Skills 的选择行为与今天逐字节一致：默认值来自账户、未改动时不发 `skill_ids`、
  加载失败时提示账户默认并允许重试。
- 点「上传文件」直接弹出系统文件选择器，中间没有任何自绘弹窗。
- 上传一个文件夹后，`/uploads` 下的相对路径结构和本地一致。
- **跑一次带上传的任务，产出的 draft PR 的 diff 里不含任何上传文件。** 这条是
  §5.6 的回归测试，必须真的跑一次，不能只靠代码审查。
- 超过单文件或合计上限时，在按运行之前就报错，并指明是哪个文件；其余文件照常附上。
- 键盘可以完成：打开菜单 → 选中 Skills → 改选择 → 应用 → 焦点回到加号。
- 新文案在 `en.ts` 和 `zh-CN.ts` 里都存在。

## 相关

- `docs/specs/2026-09-17-personal-skills.md` —— Skills 的选择与执行语义
- issue #230 —— `setup_script` 只有 API，没有界面
- issue #212 —— Codex/Kilo 不能接 MCP，界面直到创建失败才说
