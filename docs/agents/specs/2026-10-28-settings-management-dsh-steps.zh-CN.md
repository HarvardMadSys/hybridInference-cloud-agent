# Settings 管理：DSH 细分执行卡

日期：2026-10-28。依赖同目录设计文档。状态以 reviews 验收记录为准。

2026-10-28 预览反馈修订：S06–S10 原来交付的 Deployment 卡片和标签被后续改动
撤下；七项说明不再作为前端设置搜索结果。对应可编辑设置、恢复缺项提示和后台旧
bootstrap 响应保留。下表记录最初执行历史，不表示这组卡片仍是当前目标；其余
未执行卡片仍须按独立设计和验收推进。

## 执行约定

- 一次只领取一张卡或明确指定的相邻小批次；只修改卡内文件。需要扩大范围先报告理由。
- 开始先读 AGENTS.md、设计对应章节、git status；不要覆盖既有工作。
- DSH 负责小步实现；Codex 审查真实 diff、验收并整合；Claude 负责设计/独立审查。
- 不 push、不创建 PR、不合并、不部署、不修改真实凭据/全局工具配置、不访问生产数据。
- 使用生成的测试数据，不削弱已有测试；无关 lint/format 不顺手重写。
- 输出：任务 ID、改动文件、行为变化、实际执行的检查和结果、剩余问题。
- 记录“未运行/环境受阻”而不是通过。模型的完成声明不能代替独立检查。

## 第一组：部署项具有明确管理入口（S01–S12）

| ID | 依赖 | 单一动作 / 允许范围 | 验收条件 |
|---|---|---|---|
| S01 | 设计 | 新建 deployment_setup.py，迁移静态 BOOTSTRAP 声明；settings.py 保留导出兼容 | 7 个旧 key 顺序保持；不改存储/鉴权；settings.py 不增长 |
| S02 | S01 | 为七行增加稳定 id、management、requirement、固定 actions；修正说明 | GitHub 不再写 above；Compute 双来源；proxy trust 不混淆 |
| S03 | S02 | 新模块生成可选 state；只使用小范围既有配置事实 | root dedicated/fallback/missing；可选未用不标错误；无网络请求；pgAdmin 默认不误判 |
| S04 | S03 | settings_response 接入新输出；新增针对元数据、状态与无泄漏的行为测试 | 旧字段兼容；sentinel 值/路径不出现在 bootstrap；读 settings 不做外部探测 |
| S05 | S04 | lib/api/adminSettings.ts 增加可选类型 | 旧 fixture 不需被批量改写；PUT schema 原样 |
| S06 | S05 | 新建 DeploymentSetup 组件及固定 target resolver | 未知 target 不可导航；secret/目录不回显；状态是配置而不是健康 |
| S07 | S06 | settings/page.tsx 新增末尾 Deployment tab，移走共享页脚 | 页面只挂载一份；其他 editor 保持 mounted；键盘 tab 顺序正确 |
| S08 | S07 | 复用独立组件到 ApplicationSettings 的 /setup 使用处 | 恢复页仍可看部署说明；recovery token 行为不变 |
| S09 | S07 | settingsSearchIndex 改为 Deployment 定位；稳定 id 与旧锚点兼容 | 搜索 key/描述均可达；旧 hash 仍定位；未知项回退可见 |
| S10 | S06 | 将 GitHub/pgAdmin/Compute actions 接到真实页面/字段 | 点击后目标可见；Compute 只补稳定锚点与打开既有面板的小范围处理，不重写管理页面 |
| S11 | S08–10 | 新增行为测试：旧后端、未知 target、搜索、hash、draft、401/403 | 真实组件交互覆盖；不只断言源码字符串 |
| S12 | S11 | Node22 前端检查、后端相关测试、浏览器窄屏验收，写 review 记录 | 说明实际机器；未通过不得标完成；不等于部署验收 |

## 第二组：生效方式和操作说明（S13–S20）

| ID | 依赖 | 单一动作 / 允许范围 | 验收条件 |
|---|---|---|---|
| S13 | S04 | Setting 增可选 apply_mode/apply_description，默认 unverified | 旧消费者兼容；不改变实际 runtime |
| S14 | S13 | 核对 MCP 两项读取方，标 listener restart | 与真实客户端重建时机相符 |
| S15 | S13 | 核对 session/control signing、default execution profile | 分别说明登出、host 重新登记、新会话；不承诺无中断 |
| S16 | S13 | 核对常用动态读取项，逐个标 next_operation | 每一项有读取点依据；其余保留 unverified |
| S17 | S14–16 | 字段和 Review 展示 apply metadata；改保存成功提示 | 保存不等于全进程应用；旧后端仍有原说明 |
| S18 | S17 | 测试混合 apply modes、reset、secret replace、stale Review | 审核所用 revision 和 CAS 保持；反馈无 secret |
| S19 | S04 | 维护文档写管理矩阵、root fallback、部署 Token 轮换、轮换/迁库边界 | runbook 明确前提；没有未实现的命令；英中同步 |
| S20 | S18–19 | 后端/前端/docs gates，补 review 记录 | 记录命令/commit/机器；不重复无关测试 |

## 第三组：设置组织与编辑（S21–S29）

| ID | 依赖 | 单一动作 | 验收条件 |
|---|---|---|---|
| S21 | S13 | Setting 增 frequency；对一次性接入项逐个标注 | 不重新引入 Advanced 折叠；未知字段有可见默认位置 |
| S22 | S21 | category 内 routine 在前、usually_once 在后 | 只有混合分组显示分隔标题；不重复字段 |
| S23 | S22 | 提取并审核 Common 字段清单及旧路由映射 | 默认模型/任务/沙箱控制可发现；每字段唯一 editor |
| S24 | S23 | 增加 Common 默认入口和兼容导航 | 旧 category/hash 继续可用；无草稿丢失 |
| S25 | S11 | setup.groups 缺项按 key 去重、按 tab 汇总 | 可选未启用不计数；缺失 definition 有稳定回退 |
| S26 | S25 | 添加“只看待配置”筛选 | 清除筛选后恢复；隐藏编辑器不丢草稿 |
| S27 | S11 | dirty group 向 page 上报，tab 添加未保存标记 | 变回原值清除；跨 tab/轮询保留；卸载清理 |
| S28 | S27 | 普通字段原地编辑，秘密替换/清除仍显式操作 | Review+CAS 不变；无变化不出现保存栏；多组各自保存 |
| S29 | S22–28 | 交互回归、390px预览、全部前端 gates | 既有 reset/409/recovery 测试仍通过 |

## 第四组：管理员凭据（S30–S36，设计门）

先产出单独 ADR 明确权限与恢复语义，Codex/Claude 评审后再实现，禁止 DSH 自行选择：

1. S30：列出所有 verify_admin_access 使用路径与主体，实际调用测试覆盖。
2. S31：比较独立 scoped credential 与单 token 迁移，定下默认、创建权、期限和撤销规则。
3. S32：定义存储 verifier/版本、审计身份、角色撤销、灾难恢复与旧版回退行为。
4. S33：实现 schema/repository/ports；真实 PostgreSQL 验证创建、并发替换与撤销。
5. S34：鉴权接入，只在已定义路径允许；env recovery 与 on-call scope 不被扩大。
6. S35：管理界面（创建一次展示、列出、撤销）及确认；secret 不留浏览器持久存储。
7. S36：多进程撤销/DB故障/角色撤销/恢复回归；过门禁后才交付。

## 第五组：轮换、迁库、应用状态（S37–S47，分别设计门）

- S37：为 root/user/compute key 建立表列与 domain inventory；包含环境 MAC 和归档凭据。
- S38：建立各家族真实 fixture；用原 key 解密、候选 key 往返、未覆盖家族拒绝执行。
- S39：定义 key ID、operation journal、并发写围栏、进程集合与回滚边界。
- S40：实现 root key 轮换 dry-run，只报告数量与错误类别，不输出任何数据。
- S41：实现 root 实际轮换和崩溃重入；测试所有阶段失败/重启/恢复。
- S42：compute key 独立实现，保持 profile 内容逐字节不变。
- S43：user key 独立实现；MAC、快照复用和 OAuth 并发写有明确处理。
- S44：先区分同库连接参数编辑与异库迁移，设计数据库外配置存储、受控执行器、连接校验及应用/重启恢复；再实现迁移计划与预检，不向普通 API 放任意 DSN 执行能力。
- S45：一致性迁移/切换/故障回退，禁止恢复写入后的旧库分叉。
- S46：定义实例注册、角色、已读取/已应用版本、心跳与 epoch 恢复语义。
- S47：实现应用上报和 UI；离线/遗漏/未支持不能显示已生效。

## DSH 单卡提示词模板

```text
任务 Sxx；只完成该卡，不提前执行后续卡。
仓库/分支/基线：由协调者填入当前实际值。
先读 AGENTS.md、设计对应章节、卡的依赖输出和 git status。
允许修改：明确文件列表。禁止：鉴权/存储/网络/生产/真实凭据/推送。
行为目标：填卡中的单一动作。
验收：填卡中的失败场景与检查命令；没有执行的检查明确说明。
遇到设计缺口记录问题，不猜测扩大权限或添加迁移开关。
最后报告文件、行为、验证、未完成项；由 Codex 审查后领取下一卡。
```
