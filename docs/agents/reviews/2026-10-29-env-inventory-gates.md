# 环境变量盘点与门禁：实现与验证记录

- 日期：2026-10-29
- 基线：`dev@4b7b826d`（与远端 `dev` 相同）
- 提交（分支 `codex/config/env-inventory-gates`）：实现 `17ece88f`，更正 `a27ea2c2`，shell 覆盖 `5bf2800c`；本记录的更新是其后的纯文档提交
- 机器：spark2；Python 3.13.12（项目 venv）、uv 0.10.6、ruff 0.16.10、mypy 2.3.0、pytest 9.1.1
- 设计与盘点：[2026-10-29-env-inventory-and-gates.zh-CN.md](../specs/2026-10-29-env-inventory-and-gates.zh-CN.md)

## 改了什么

| 文件 | 内容 |
|---|---|
| `tests/support/env_scan.py` | AST 扫描器和模板、前端名字收集 |
| `tests/support/env_shell.py` | 有界的 shell 扫描器：`deploy/`、`scripts/` 下 11 个 `.sh` 消费的外部输入 |
| `tests/support/env_inventory.py` | 分类表（44 个族、327 个精确名、6 个前缀）、五张 Python 精确例外表和三张 shell 例外表 |
| `tests/unit/test_env_classification.py` | 10 个仓库门禁 + 14 个反例测试 |
| `tests/unit/test_env_scan.py` | 9 个扫描器自测（其中 4 个针对 shell） |
| `docs/agents/specs/2026-10-29-env-inventory-and-gates.zh-CN.md` | P1 修正版 |

没有改产品代码、配置默认值、Compose、维护中的指南或 CI 工作流；新测试随现有 `make test` 和 CI 单元测试分片运行。
分类表只在 `tests/` 下，不进入镜像。

## 验证

1. 聚焦测试：`uv run pytest -q tests/unit/test_env_scan.py tests/unit/test_env_classification.py
   tests/unit/test_environment_inventory.py tests/unit/test_ci_shard.py tests/unit/test_ci_shards_workflow.py
   tests/unit/test_ci_tests_ran.py`：86 passed。
2. 真实代码树上的变异检查：每次往真实文件里注入一种违规，跑门禁，再 `git checkout` 恢复。

   | 编号 | 注入 | 失败的测试 |
   |---|---|---|
   | M1 | `entitlement.py` 里裸读 `AGENT_REPO_ALLOWLIST` | `test_no_backend_code_reads_a_setting_past_its_resolver` |
   | M2 | 把 `AGENT_MAX_STREAMS_PER_USER` 注册进 `SETTINGS`（不进 `MIGRATED_SETTINGS`） | `test_registries_match_the_table`、`test_no_backend_code_reads_a_setting_past_its_resolver`、`test_new_environment_imports_are_migrated` 等 5 个 |
   | M3 | runner 读一个新变量 `AGENT_BRAND_NEW_KNOB` | `test_every_variable_is_classified` |
   | M4 | `deploy/host/.env.example` 加 `AGENT_MCP_TOKEN=` | `test_internal_and_tooling_names_stay_in_their_place` |
   | M5 | 控制面读主机设置 `AGENT_TIMEOUT_S` | `test_no_backend_code_reads_a_setting_past_its_resolver` |
   | M6 | runner 新函数里 `os.environ.get(setting.env)` | `test_every_read_names_its_variable` |
   | M7 | 控制面新函数把 `dict(os.environ)` 交给子进程 | `test_every_read_names_its_variable` |

   恢复后门禁 20 passed，工作区只剩本 PR 的新文件。
3. 完整门禁：`umask 022; env -u CODEX_VERSION PYTEST_ADDOPTS='-n 4 --dist loadfile' make check`：在 `17ece88f` 上通过（格式化未改动文件；ruff、mypy 563 个源文件无问题；13169 passed、7 skipped）。更正提交 `a27ea2c2` 改了分类表里的一句理由和规格里的一句说明，又完整跑了一遍，结果相同。日志在交付目录 `evidence/make-check.log`。

前端、数据库和文档站点的检查不适用：没有改前端、存储、SQL、迁移或站点页面（`docs/agents/` 不在站点里）。

## 限制

扫描器是语法近似，支持和不支持的读法见规格 §6.1、§6.2；shell 脚本、`tests/` 和第三方库自己读的变量不在门禁内。
`UNMIGRATED_ENV_SETTINGS` 是冻结表，只能保证新注册走对列表，不能证明历史导入行为。
门禁扫描整个仓库约 3 秒。

## 补充：shell 脚本覆盖（`5bf2800c`）

第一次交付把 shell 脚本排除在门禁外；这次补上（规格 §6.4）。扫描 11 个脚本，得到 116 个外部输入，
其中 36 个只出现在 shell 里，新增 5 个族归类；没有计算读取。不修改任何部署脚本。

1. 聚焦测试：上面的 6 个文件加 `tests/unit/test_compat_markers.py`，109 passed。
2. 真实代码树上的 shell 变异检查（日志 `evidence/shell-mutation-checks.log`），每次注入后恢复：

   | 编号 | 注入 | 结果 |
   |---|---|---|
   | S1 | `deploy/host/deploy.sh` 读一个新的 `${AGENT_NEW_DEPLOY_KNOB:-}` | `test_every_variable_is_classified` 失败 |
   | S2 | `sandbox_host.sh` 经 `env_or_dotenv` 读一个新名字 | `test_every_variable_is_classified` 失败 |
   | S3 | 控制面 `deploy.sh` 加普通局部变量、`$1`、`${BASH_SOURCE[0]}` | 全部通过（不误报） |
   | S4 | `test-db-local.sh` 加一个计算出来的 `${!picked}` | `test_every_shell_read_names_its_variable` 失败 |
   | S5 | `artifact_env.sh` 在已豁免的 `SHA` 旁边读一个新名字 | 新名字被报出，`SHA` 的豁免不覆盖它 |

3. 完整门禁：在 `5bf2800c` 上 `umask 022; env -u CODEX_VERSION PYTEST_ADDOPTS='-n 4 --dist loadfile' make check`
   通过（格式化未改动文件；ruff、mypy 563 个源文件无问题；13178 passed、7 skipped），日志
   `evidence/make-check-final.log`；之前两次的日志保留在 `evidence/make-check.log`。

shell 扫描是词法近似，不是解释器；看不到的写法（调用方赋值、source 进来的值、交给另一个 shell 的字符串、
用 `grep`/`sed` 从 `.env` 取值、函数内的执行顺序）见规格 §6.4 末尾。
