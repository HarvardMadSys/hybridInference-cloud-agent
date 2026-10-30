# 环境变量盘点：按统一前端管理修订 P1（验证记录）

- 日期：2026-10-30（修订于 2026-10-29 开始，因额度中断，2026-10-30 续完）
- 基线：分支 `codex/config/env-inventory-gates@322a8fd0`；盘点数字的基线仍是 `dev@4b7b826d` 加本分支扫描器
- 机器：freeinference-gpu（rtx6000b）；Python 3.13.13（项目 `.venv`，`uv sync --frozen` 建立）、uv 0.13.6
- 性质：用户纠正产品目标后的文档修订。没有产品代码改动，没有实现计划中的任何工作包。

## 用户的纠正

目标是让尽可能多的配置能在前端管理；当前来源和目标管理方式要分开；要有具体的实施计划。
上一版 P1 把「今天由部署环境提供」写成「应当留在部署环境」「永不成为 admin 设置」，并说只有两项能搬。

## 改了什么

| 文件 | 内容 |
|---|---|
| `docs/agents/specs/2026-10-29-env-inventory-and-gates.zh-CN.md` | 重写结论和建议：两个轴（§4）、多读者按含义建模（§5.2）、门禁与目标方式的关系（§6.5）、44 个族的两轴映射（§7）、控制与恢复边界（§8）、统一 Admin 体验（§9）；附录 A 加「族」「目标方式」两列 |
| `docs/agents/specs/2026-10-30-unified-config-management-plan.zh-CN.md` | 新增分阶段实施计划：12 个工作包、依赖、契约、纵向切片、升级、失败与验收 |
| `tests/support/env_inventory.py` | 只改说明文字：模块说明、分类注释，以及 F19（`ADMIN_TOKEN` 授予完整管理员权限）、F20（「never accepted」→「today」）、F28（「never becomes an admin setting」→ 不得放宽）三条族理由 |

保留不变：§3 的全部数字及基线、§5.1、§5.3、§6.1–§6.4 原文、例外表、已知欠账、附录 A 每行的名称、分类和出现位置。

目标方式的分布（369 个唯一名）：编辑 88、应用 90、流程 17、观测 50、对象 40、不开放 84。

## 验证

1. 分类表只改说明：在本机加载基线和修订后的 `env_inventory.py`，44 个族的（分类、名字、前缀）完全相同，
   9 张例外表完全相同，只有 F19、F20、F28 的理由文字不同（`evidence/scope-check.log`）。
2. 聚焦测试：`env -u CODEX_VERSION .venv/bin/python -m pytest -q tests/unit/test_env_scan.py
   tests/unit/test_env_classification.py tests/unit/test_environment_inventory.py tests/unit/test_compat_markers.py`：
   54 passed（`evidence/focused-tests.log`）。
3. `ruff format --check`、`ruff check`、`mypy` 对 `tests/support/env_inventory.py`：通过（`evidence/lint.log`）。
4. P1 保留检查：基线 §5.1、§5.3、§6.1–§6.4 原文逐字出现在修订稿中；附录 A 的 369 行名称、分类、出现位置与基线一致；
   §3 的计数都在（`evidence/p1-preservation.log`）。附录的「族」「目标方式」两列由脚本按分类表生成，脚本断言每个名字
   恰好属于一个族且分类一致。
5. `git diff --check` 无问题；改动只在上表三个文件。

没有运行完整的 `make check`：门禁逻辑、例外表和产品代码都没变，只改了文档和字符串。没有运行前端、数据库和文档站点检查：
没有改前端、存储或站点页面（`docs/agents/` 不在站点里）。

## 限制

- 两轴映射中的「目标」都是提议；只有族一级证据的地方没有细到单个变量，附录里少数与族不同的名字在 §7 写了原因。
- 没有查看任何生产配置；导入欠账对实际部署的影响仍未知。
- 计划中的部署代理、实例上报、轮换和迁库都未实现，也未经设计评审。
