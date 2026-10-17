# Translating the docs

This page is for translators, and for anyone whose English edit changes a
paragraph that has a translation. The site is published in English and
Simplified Chinese; the Chinese pages are at `/zh_CN/` and the sidebar
switches between them.

The pages are written in English and translated with Sphinx's gettext
workflow, so a translation is attached to each paragraph of source text rather
than to a whole file. Any string without a translation falls back to English,
so the site always builds complete.

```bash
make docs-gettext                      # extract one catalog template per page
make docs-translate DOCS_LANG=zh_CN    # create or update that language's catalogs
# edit docs/developer/locale/zh_CN/LC_MESSAGES/*.po -- fill in msgstr
make docs-lang DOCS_LANG=zh_CN         # build that language alone and read it
make docs-verify                       # what CI runs: the full site plus checks
```

A catalog entry pairs the English source with its translation:

```po
msgid "Cloud Agent runs coding agents against a user's repository inside retained sandbox containers."
msgstr "Cloud Agent 在保留的沙箱容器中，针对用户的仓库运行编码 agent。"
```

The English text is the lookup key, so editing a paragraph invalidates its
translation automatically: the page falls back to English for that paragraph
instead of serving a translation that no longer matches what the code does.
That is the reason to use catalogs rather than parallel `.zh-CN.md` files,
which drift silently.

Commit the `.po` files. `docs/gettext/` and compiled `.mo` files are generated
and ignored.

## When you change an English page

Run `make docs-translate` and translate the entries it adds or marks
`#, fuzzy`, then delete the `#, fuzzy` line and any obsolete `#~` entries.
`make docs-verify` fails until every English string has a current
translation. If you cannot translate a change yourself, say so in the pull
request; a reviewer who can will add it.

## Chinese style

Write the Chinese an experienced colleague would write to explain the page,
not a sentence-by-sentence rendering of the English. Read the whole section
first, work out what it is telling the reader, and then say that in Chinese.
A reader should not be able to tell which language the page was written in.

**Keep every fact.** Natural is not looser: a condition, a number, a default,
an order of steps or a warning in the English stays in the Chinese, with the
same strength. Do not add explanations the English does not give.

**Rebuild long sentences.** English hangs conditions and details off a noun
in relative clauses; Chinese states the condition first and then the result,
and splits a long sentence into short ones. A string of 的 in front of a noun
is the usual sign that a clause should become its own sentence.

**Drop the pronouns and filler English needs.** Most *it*, *its* and *they*
disappear in Chinese or become the noun they stand for. Leave out 进行, 一个
in front of every noun, 能够 and 从而 unless they carry meaning. Prefer the
active voice; use 被 only when the passive is the point.

**Translate the meaning, not the word.** *That exact host* is 这台主机, not
那台确切的主机; *runs agents against a repository* is 处理仓库; *buys
inference* is 调用模型. When an English idiom has no direct Chinese
equivalent, say plainly what it means.

**Talk to the reader.** Address the reader as 你 in instructions, and use 请
sparingly. Spell out what to do in the order they will do it.

| Stiff | Natural |
|---|---|
| Cloud Agent 在保留的沙箱容器中针对用户的仓库运行编码 agent。 | Cloud Agent 让编码 agent 在保留的沙箱容器里处理用户的仓库。 |
| 编排器会等待，直到那台确切的主机仍报告 `draining`。 | 编排器会一直等到这台主机报告 `draining`。 |
| 授权部署就绪检查只接受这一台被命名的排空主机。 | 这样部署的就绪检查只会放行这一台正在排空的主机。 |

## Chinese traps

The first three are silent: the build stays green and the page is wrong. The
structural check in `make docs-verify` catches them.

**Keep code, link targets and markup exactly.** Inline code, URLs, link
targets, anchors, environment variable names and command lines stay as they
are. Translate a link's text, never its target. The check compares the inline
code literals and link targets of each translated page with the English one.

**A heading that starts with a number is dropped.** MyST reads `1. ` as a list
marker, the structure no longer matches the source, and Sphinx emits the
English heading. Escape the period: `msgstr "1\. 申请一个节点"`.

**Emphasis next to Chinese punctuation.** A closing `**` preceded by a Chinese
full stop and followed by a Chinese character is not recognised, so
`**术语。**后文` shows literal asterisks. Put the punctuation outside the bold:
`**术语**。后文`. The same applies when bold text starts or ends with code or a
quotation mark: ``**`AGENT_TIMEOUT_S`**的默认值`` breaks, so add a space,
``**`AGENT_TIMEOUT_S`** 的默认值``.

**A stale `.mo` masks your edits.** Sphinx skips recompiling when the `.mo` is
newer than its `.po`. Before checking a build, run
`find docs/developer/locale -name '*.mo' -delete`.

## Chinese terminology

Use the same Chinese for a term on every page. Product and component names,
UI labels shown in bold, API fields, states and command names stay in English.

| English | Chinese |
|---|---|
| control plane | 控制平面 |
| host, execution host | 主机、执行主机 |
| sandbox, sandbox group | 沙箱、沙箱组 |
| retained sandbox | 保留沙箱 |
| conversation, chat | 对话 |
| task | 任务 |
| turn | 轮次（一轮） |
| follow-up | 后续消息 |
| workspace | 工作区 |
| snapshot | 快照 |
| publish, publication | 发布 |
| checkpoint | 检查点 |
| archive | 归档 |
| enroll, enrollment | 注册 |
| heartbeat | 心跳 |
| lease | 租约 |
| claim | 认领 |
| drain | 排空 |
| fence | 栅栏 |
| adopt (an issuer or existing data) | 认定 |
| take over, takeover | 接管 |
| checkout (noun / verb) | 本地仓库、仓库副本 / 检出 |
| stack | 一套服务、环境 |
| provider | 服务商 |
| inference grant | 推理授权 |
| credential | 凭据 |
| token | 令牌 |
| relay, tunnel | 中继、隧道 |
| bridge | 网桥 |
| resolver | 解析器 |
| owner | 所有者 |
| member | 成员 |
| operator | 运维人员 |
| administrator | 管理员 |
| deployment | 部署 |
| distribution | 发行版 |
| migration | 迁移 |
| native session | 原生会话 |
| gateway | 网关（HybridInference Gateway） |
| runner, Sandbox Manager, Fork, Kata, MCP, attempt, residency, execution profile | runner、Sandbox Manager、Fork、Kata、MCP、attempt、residency、execution profile |
