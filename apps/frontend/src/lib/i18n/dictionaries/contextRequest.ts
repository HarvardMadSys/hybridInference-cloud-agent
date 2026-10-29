// One request's composition in the Context view. The words keep three things
// apart: the provider's reported input, the CLI's own account of its context,
// and stored content reconstructed in transcript order.

export const contextRequestEn = {
  'contextRequest.title': 'What this request carried',
  'contextRequest.loading': 'Reading this request…',
  'contextRequest.error': 'Could not read this request’s composition.',
  'contextRequest.noEvent': 'This request has no stored event to anchor its composition.',
  'contextRequest.provider': 'Reported input',
  'contextRequest.providerDetail':
    '{read} read from cache · {write} written to cache · {uncached} uncached. The cache split is how this input was billed, not more content.',
  'contextRequest.native': 'The CLI’s own account',
  'contextRequest.native.turnStart':
    'Read when turn {turn} started: what this request was sent with, as the CLI’s server listed it.',
  'contextRequest.native.turnStartLater':
    'Read when turn {turn} started, before this request. Requests of this turn since then: {count}; what they added is not in it.',
  'contextRequest.native.turnStartCompacted':
    'A compaction later in this turn replaced part of what is listed here.',
  'contextRequest.native.estimate': 'Sizes estimated from the text the CLI listed.',
  'contextRequest.native.namesOnly':
    'Named by Claude Code when turn {turn} started. It reports sizes only when asked, and answering costs token-counting calls to the model provider while the conversation waits, so the platform does not ask.',
  'contextRequest.native.named': '{count} named',
  'contextRequest.native.mcpServers': 'MCP servers: {servers}',
  'contextRequest.native.notExposed': 'Not exposed by this CLI',
  'contextRequest.native.not_collected':
    'No CLI account for this turn: it ran before collection began, or the read did not finish while the turn ran.',
  'contextRequest.native.not_settled':
    'No CLI account: this attempt was retried, failed or was cancelled before the read arrived.',
  'contextRequest.native.pending':
    'The CLI’s server is read as the turn starts; the read has not arrived yet.',
  'contextRequest.native.not_offered':
    '{runtime} offers no supported way to read its context, so only the reconstruction below is available.',
  'contextRequest.native.not_offered.pi':
    'pi answers questions about its context only over this conversation’s own channel, so the platform does not ask and a turn never waits for it. Only the reconstruction below is available.',
  'contextRequest.native.failed': 'The CLI’s account could not be read ({status}).',
  'contextRequest.native.omitted': '{count} smaller items are not listed; the totals include them.',
  'contextRequest.reconstructed': 'Reconstructed from the stored conversation',
  'contextRequest.reconstructed.note':
    'Stored content in the order it was recorded, selected for this request. It is not the assembled request: what the CLI adds itself (system prompt, tool definitions, instructions) is not here, stored previews can be clipped, and sizes are estimates.',
  'contextRequest.reconstructed.total': '≈{tokens} estimated from stored text',
  'contextRequest.reconstructed.boundary': 'Starts after the compaction in turn {turn}.',
  'contextRequest.reconstructed.preserve':
    'The CLI can keep some recent messages through a compaction; they are counted as compacted here, so this can show less than the request carried.',
  'contextRequest.excluded': 'Not counted',
  'contextRequest.excluded.compacted': 'Before the compaction: {count}',
  'contextRequest.excluded.superseded_attempts': 'From retried attempts: {count}',
  'contextRequest.excluded.subagent': 'In subagents’ own contexts: {count}',
  'contextRequest.excluded.this_request_output': 'This request’s own reply: {count}',
  'contextRequest.copied':
    'Turns copied from the original conversation: {count}. The CLI carries them; they are stored with that conversation.',
  'contextRequest.capped': 'Only the most recent stored items were read.',
  'contextRequest.filter': 'Show',
  'contextRequest.filter.all': 'All',
  'contextRequest.entry.prompt': 'Turn {turn} prompt',
  'contextRequest.entry.turn': 'turn {turn}',
  'contextRequest.entry.clipped': 'clipped',
  'contextRequest.entry.show': 'Show content',
  'contextRequest.entry.hide': 'Hide content',
  'contextRequest.entry.more': 'Load more',
  'contextRequest.entry.loading': 'Loading…',
  'contextRequest.entry.of': '{shown} of {total} characters',
  'contextRequest.entry.storedClipped': 'This is the clipped preview the platform stored.',
  'contextRequest.category.system_prompt': 'System prompt',
  'contextRequest.category.tool_definitions': 'Tool definitions',
  'contextRequest.category.mcp_tools': 'MCP tools',
  'contextRequest.category.instructions': 'Instructions and memory files',
  'contextRequest.category.skills': 'Skills',
  'contextRequest.category.agents': 'Agent definitions',
  'contextRequest.category.injected': 'Injected context',
  'contextRequest.category.compaction_summary': 'Compaction summary',
  'contextRequest.category.user_messages': 'User messages',
  'contextRequest.category.assistant': 'Answers',
  'contextRequest.category.reasoning': 'Reasoning',
  'contextRequest.category.tool_calls': 'Tool calls',
  'contextRequest.category.tool_results': 'Tool results',
  'contextRequest.category.messages': 'Messages',
  'contextRequest.category.other': 'Other',
} as const;

export const contextRequestZhCN: Record<keyof typeof contextRequestEn, string> = {
  'contextRequest.title': '这次请求带了什么',
  'contextRequest.loading': '正在读取这次请求…',
  'contextRequest.error': '无法读取这次请求的构成。',
  'contextRequest.noEvent': '这次请求没有可以对应的存储事件。',
  'contextRequest.provider': '服务商报告的输入',
  'contextRequest.providerDetail':
    '其中缓存读取 {read}，写入缓存 {write}，未缓存 {uncached}。这只是这部分输入的计费拆分，不是额外的内容。',
  'contextRequest.native': 'CLI 自己的统计',
  'contextRequest.native.turnStart':
    '在第 {turn} 轮开始时读取：CLI 的服务端列出的、这次请求发送的内容。',
  'contextRequest.native.turnStartLater':
    '在第 {turn} 轮开始时读取，早于这次请求。此后本轮的请求数：{count}；它们新增的内容不在其中。',
  'contextRequest.native.turnStartCompacted': '本轮稍后发生了压缩，这里列出的部分内容已被替换。',
  'contextRequest.native.estimate': '大小按 CLI 列出的文本估算。',
  'contextRequest.native.namesOnly':
    '第 {turn} 轮开始时 Claude Code 列出的名称。它只在被询问时才报告大小，而回答会在对话等待期间向模型服务商发起计算 token 的请求，所以平台不询问。',
  'contextRequest.native.named': '{count} 个名称',
  'contextRequest.native.mcpServers': 'MCP 服务：{servers}',
  'contextRequest.native.notExposed': '这个 CLI 不提供',
  'contextRequest.native.not_collected':
    '这一轮没有 CLI 统计：它早于采集开始，或者读取没有在这一轮运行期间完成。',
  'contextRequest.native.not_settled':
    '没有 CLI 统计：这次尝试在读取完成之前被重试、失败或被取消。',
  'contextRequest.native.pending': 'CLI 的服务端在轮次开始时读取，结果还没有到达。',
  'contextRequest.native.not_offered':
    '{runtime} 没有受支持的方式读取它的上下文，所以只有下面的重建结果。',
  'contextRequest.native.not_offered.pi':
    'pi 只能通过这个对话自己的通道回答上下文问题，所以平台不询问，轮次也从不为此等待。只有下面的重建结果。',
  'contextRequest.native.failed': '无法读取 CLI 的统计（{status}）。',
  'contextRequest.native.omitted': '另有 {count} 个较小的项目没有列出，合计已包含它们。',
  'contextRequest.reconstructed': '根据已存储的对话重建',
  'contextRequest.reconstructed.note':
    '按记录顺序挑出这次请求之前已存储的内容。这不是实际发出的请求：CLI 自己加入的内容（系统提示词、工具定义、指令）不在这里，存储的预览可能被截短，大小也是估算的。',
  'contextRequest.reconstructed.total': '按存储文本估算约 {tokens}',
  'contextRequest.reconstructed.boundary': '从第 {turn} 轮的压缩之后开始。',
  'contextRequest.reconstructed.preserve':
    'CLI 压缩时可能保留最近的一些消息；这里把它们算作已压缩，所以显示的内容可能比请求实际带的少。',
  'contextRequest.excluded': '没有计入',
  'contextRequest.excluded.compacted': '压缩之前的 {count} 项',
  'contextRequest.excluded.superseded_attempts': '被重试替换的尝试中的 {count} 项',
  'contextRequest.excluded.subagent': '子 Agent 自己上下文里的 {count} 项',
  'contextRequest.excluded.this_request_output': '这次请求自己回复的 {count} 项',
  'contextRequest.copied': '从原会话复制来的 {count} 轮由 CLI 带着，但存储在原会话里。',
  'contextRequest.capped': '只读取了最近存储的内容。',
  'contextRequest.filter': '显示',
  'contextRequest.filter.all': '全部',
  'contextRequest.entry.prompt': '第 {turn} 轮的提问',
  'contextRequest.entry.turn': '第 {turn} 轮',
  'contextRequest.entry.clipped': '已截短',
  'contextRequest.entry.show': '查看内容',
  'contextRequest.entry.hide': '收起内容',
  'contextRequest.entry.more': '加载更多',
  'contextRequest.entry.loading': '正在加载…',
  'contextRequest.entry.of': '{total} 个字符中的 {shown} 个',
  'contextRequest.entry.storedClipped': '这是平台存储的截短预览。',
  'contextRequest.category.system_prompt': '系统提示词',
  'contextRequest.category.tool_definitions': '工具定义',
  'contextRequest.category.mcp_tools': 'MCP 工具',
  'contextRequest.category.instructions': '指令与记忆文件',
  'contextRequest.category.skills': '技能',
  'contextRequest.category.agents': 'Agent 定义',
  'contextRequest.category.injected': '注入的内容',
  'contextRequest.category.compaction_summary': '压缩摘要',
  'contextRequest.category.user_messages': '用户消息',
  'contextRequest.category.assistant': '回答',
  'contextRequest.category.reasoning': '推理',
  'contextRequest.category.tool_calls': '工具调用',
  'contextRequest.category.tool_results': '工具结果',
  'contextRequest.category.messages': '消息',
  'contextRequest.category.other': '其他',
};
