# 一次性导入 FreeInference 用户

日期：2026-11-05。状态：已实现，和本文同一个 PR。

相关：[账号与等待名单](../../developer/local-accounts.md#importing-accounts-from-freeinference)；[账号功能对齐](2026-10-29-account-feature-parity.zh-CN.md)。

## 1. 结论

- Cloud Agent 有了独立账号系统（#1030）以后，FreeInference 的老用户要重新注册才能用。为了给产品一批初始用户，把 FreeInference 里**状态正常**的用户**一次性**拷进来，直接设为**已批准**。
- 用户无感：用原来的邮箱、登录名或手机号和原来的密码直接登录。不发邮件，不弹服务条款，也不要求重置密码。
- 只导这一次，之后两边没有任何关系：不同步，不保留对应关系，FreeInference 封号或删号也不影响 Cloud Agent。
- 导入时不配置模型，用户登录后自己添加 model connection。
- 只改 Cloud Agent，网关不改代码。导出用一条只读 SQL，导入用一个运维命令。

## 2. 为什么能直接拷密码哈希

两边的密码哈希都是 Argon2id，参数相同：64 MiB 内存、3 轮、4 路并行。FreeInference 用 passlib，Cloud Agent 直接调 argon2-cffi（`accounts/passwords.py` 就是从网关改写过来的），生成的都是 `$argon2id$v=19$m=65536,t=3,p=4$...` 格式。集成测试里用的哈希就是由 FreeInference 的 `hash_password` 生成的，在 Cloud Agent 用原密码登录成功。

## 3. 导什么，不导什么

| FreeInference `users` | 处理 |
|---|---|
| `status = 'active'` 且没有待硬删除 | 导入 |
| `pending_approval`、`rejected`、`suspended`、`deleted` | 不导 |
| 邮箱，已验证 | 导入，记为运维人员确认（`operator`），算作已证明 |
| 邮箱，未验证 | 导入，仍为未验证 |
| 手机号，已验证 | 导入（E.164 格式） |
| 手机号，未验证 | 丢弃：这边的手机号必须是已证明的 |
| 登录名 | 有可用密码时导入，没有密码就丢弃（没有密码的登录名登不进来） |
| 密码哈希，Argon2id | 原样导入 |
| 密码哈希，其他格式 | 丢弃，用户可以通过邮件重置 |
| 显示名 | 1 到 50 个字符、不含控制字符时导入，否则丢弃 |

一行里一个能用的登录标识都没有时，跳过这一行。

## 4. 冲突和重复执行

- 只要有一个标识（邮箱、登录名或手机号）已经被 Cloud Agent 的账号占用，**整个账号跳过**，已有账号的密码和审批状态都不动。不会出现"一半标识进了新账号"的情况。
- 同一个标识在输入里出现两次，后一次跳过。
- 所以命令可以放心再跑一遍，不会重复建号。

## 5. 一致性和审计

每个账号的创建和批准在**同一个事务**里完成（`PostgresLocalAccountRepository.import_account`），写一条审计 `agent.local_account.import`，操作者为 `account-import`。命令中途停下，每个账号要么不存在，要么已批准，不会卡在"已创建但待审批"。审计只记标识的种类和有没有密码，不记邮箱、手机号或哈希。

## 6. 执行步骤

1. 在 FreeInference 数据库上执行文档里的只读导出 SQL，得到 `users.jsonl`。文件权限设为 0600。
2. 用 SSH 把文件传到 Cloud Agent 控制平面。
3. 先跑预览：`python -m cloud_agent.accounts.import_accounts users.jsonl`，确认导入和跳过的数量。
4. 正式执行：加上 `--apply`。
5. 用一个 FreeInference 账号的原密码登录验证一下，然后删除导出文件。

先在 staging 用真实导出演练一次，再在 prod 切换到新账号系统时执行。

## 7. 不做的事

- 不建 FreeInference 和 Cloud Agent 账号的对应关系，不做持续同步。
- 不给网关加接口，也不让两边共享任何密钥或会话。
- 不自动配置 FreeInference 的 model connection。
- 不记录导入用户同意了服务条款（`tos_accepted_at` 保持为空），登录时也不弹同意框。
