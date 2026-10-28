# Vendored protocol kernels

The Responses ↔ Chat Completions and Anthropic Messages ↔ Chat Completions
translation kernels in this directory were copied from
[HarvardMadSys/hybridInference](https://github.com/HarvardMadSys/hybridInference)
at commit `df40a9a82a6d5a39a9ffbdfebde3650f94cb5146`, under the MIT license
reproduced in `LICENSE.hybridinference` (Copyright (c) 2026 Harvard SEAS).

| This file | Upstream file | Changes |
|---|---|---|
| `responses_chat.py` | `apps/backend/serving/responses_translator.py` | Relative imports; `_content_parts_to_text_or_blocks` and `responses_input_to_messages` had a helper (`_image_part_to_block`, `_append_input_item`) split out, unchanged in behavior. |
| `messages_chat.py` | `apps/backend/serving/adapters/anthropic_translator.py` | Relative import; `normalize_inline_system` and `_translate_message` had a helper (`_split_inline_system`, `_bucket_block`) split out, unchanged in behavior; `extract_anthropic_usage_from_sse` (Gateway billing) removed. **Behavior change:** an image in a `tool_result` is kept as an image part (`_tool_result_images`) instead of the `[image]` placeholder; the ported test `test_request_tool_result_image_block_is_kept_not_replaced` asserts the new behavior. |
| `_messages.py` | `apps/backend/serving/utils/messages.py` | None. |
| `_token_utils.py` | `apps/backend/serving/utils/token_utils.py` | `extract_reasoning_tokens` and `extract_cache_tokens` rewritten over one lookup helper (`_first_int`, `_nested_cache_read`), same lookups in the same order. |

The helpers were split only to meet this repository's complexity limits
(McCabe 15, 12 branches), which forbid suppressions in new code. The
upstream regression tests, ported with import changes only (except the one
behavior change noted above), live in
`tests/unit/protocol_compat/test_*_upstream.py` and must keep passing.

Not vendored: `serving/adapters/claude_format.py` (the opposite direction,
a Chat client calling a Claude upstream) and `serving/utils/tokens.py`
(tiktoken estimates, which no path here needs). The Gateway's HTTP routers,
authentication, persistence, routing and billing are not part of this
package and are never imported.

**This is a copy, not a shared dependency.** The HybridInference repository
was not changed or published for it. To take an upstream fix: diff the
upstream file at its new commit against the commit above, apply the change
here by hand, keep the split helpers, update the commit in this file, and run
the ported tests plus `tests/unit/protocol_compat/`.

Everything else here is Cloud Agent's own: `gate.py` (refuses what a Chat
upstream cannot carry rather than dropping it), `stream.py` (never reports
success for an upstream stream that did not finish), `namespaces.py` and
`tool_images.py` (a tool's images, carried in the user message after its tool
messages, since a tool message holds text only). The
member-private launcher and loopback service, which do the I/O, are the host's
`cloud_agent_host/protocol_translator.py`; the helper zipapp carries it with
this package. See
`docs/agents/specs/2026-10-28-model-protocol-compatibility.zh-CN.md`.
