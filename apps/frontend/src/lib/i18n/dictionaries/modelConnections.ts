export const modelConnectionsEn = {
  'connections.title': 'Personal model connections',
  'connections.description':
    'Use your own OpenAI-compatible provider for your tasks. Connections are private to you; your provider bills your API key separately from platform inference.',
  'connections.add': 'Add connection',
  'connections.empty': 'No personal connections yet.',
  'connections.unavailable': 'Personal model connections are not available on this deployment yet.',
  'connections.loadError': 'Could not load your connections.',
  'connections.error': 'The request failed. Check the connection details and try again.',
  'connections.name': 'Connection name',
  'connections.url': 'Base URL',
  'connections.urlHint':
    'Use the provider’s API base, such as https://api.example.com/v1. Public IPv4 HTTPS on port 443 is supported.',
  'connections.key': 'API key',
  'connections.keyHint': 'Saved keys cannot be displayed. Leave blank to keep your current key.',
  'connections.secretHint':
    'The agent and code it runs can access this key inside your sandbox. Use a dedicated key with limits set at your provider.',
  'connections.protocols': 'Supported APIs',
  'connections.chat': 'Chat Completions · OpenCode, Kilo, pi',
  'connections.responses': 'Responses · Codex',
  'connections.protocolHint':
    'Choose APIs your provider supports. Claude Code requires Anthropic Messages and cannot use these connections.',
  'connections.models': 'Model IDs',
  'connections.modelsHint':
    'One model ID per line, exactly as your provider names it. You can discover additional IDs after saving.',
  'connections.edit': 'Edit',
  'connections.save': 'Save connection',
  'connections.cancel': 'Cancel',
  'connections.busy': 'Working…',
  'connections.enabled': 'Enabled',
  'connections.disabled': 'Disabled',
  'connections.enable': 'Enable',
  'connections.disable': 'Disable',
  'connections.delete': 'Delete',
  'connections.confirmDelete':
    'Delete {name}? Chats using it will need another model connection. Keys your tasks already used stay encrypted with those workspaces’ history so their output can still be scanned for them. This does not revoke the key at your provider.',
  'connections.confirmDisable':
    'Disable {name}? Its running agent processes will stop and its queued turns cannot start until it is enabled.',
  'connections.confirm': 'Confirm',
  'connections.editHint':
    'Changing the endpoint, key or APIs restarts agent processes using the connection. Removing a model prevents new turns from using it.',
  'connections.check': 'Test model',
  'connections.checkHint':
    'Runs a streaming tool call and response on the selected model and API. Your provider may charge for this test.',
  'connections.checkModel': 'Model to test',
  'connections.checkProtocol': 'API to test',
  'connections.checkPassed': 'Test passed',
  'connections.checkFailed': 'Test failed',
  'connections.checkStale':
    'The connection changed during this test. Test its current settings again.',
  'connections.checkScope':
    '{model} · {protocol}. Other models and APIs have not been verified by this test.',
  'connections.discover': 'Discover model IDs',
  'connections.discoverHint':
    'Discovery only lists IDs; it does not verify that a model can run an agent.',
  'connections.discoverEmpty':
    'The provider did not list any model IDs. You can still enter them manually.',
  'connections.addModels': 'Add selected IDs',
  'connections.checkDetails':
    'Streaming: {streaming} · Tool call: {tool} · Final response: {final}',
  'connections.yes': 'Passed',
  'connections.no': 'Not verified',
} as const;

export const modelConnectionsZhCN: Record<keyof typeof modelConnectionsEn, string> = {
  'connections.title': '个人模型连接',
  'connections.description':
    '使用自己的 OpenAI 兼容服务运行任务。连接仅自己可用；推理费用由服务商向你的 API key 计费，与平台推理额度分开。',
  'connections.add': '添加连接',
  'connections.empty': '还没有个人模型连接。',
  'connections.unavailable': '当前部署尚未开放个人模型连接。',
  'connections.loadError': '无法加载模型连接。',
  'connections.error': '操作失败，请检查连接信息后重试。',
  'connections.name': '连接名称',
  'connections.url': 'Base URL',
  'connections.urlHint':
    '填写服务商的 API 基础地址，例如 https://api.example.com/v1。支持公网 IPv4 的 HTTPS 443 端口。',
  'connections.key': 'API key',
  'connections.keyHint': '已保存的密钥不可查看。留空可保留当前密钥。',
  'connections.secretHint':
    'Agent 及其运行的代码可以在你的沙箱内读取此密钥。建议使用专用密钥，并在服务商处设置额度限制。',
  'connections.protocols': '支持的 API',
  'connections.chat': 'Chat Completions · OpenCode、Kilo、pi',
  'connections.responses': 'Responses · Codex',
  'connections.protocolHint':
    '选择服务商实际支持的 API。Claude Code 需要 Anthropic Messages，不能使用这些连接。',
  'connections.models': '模型 ID',
  'connections.modelsHint': '每行一个，须与服务商的模型 ID 一致。保存后还可发现其他模型 ID。',
  'connections.edit': '编辑',
  'connections.save': '保存连接',
  'connections.cancel': '取消',
  'connections.busy': '处理中…',
  'connections.enabled': '已启用',
  'connections.disabled': '已停用',
  'connections.enable': '启用',
  'connections.disable': '停用',
  'connections.delete': '删除',
  'connections.confirmDelete':
    '删除 {name}？使用此连接的会话需要选择其他模型连接。任务已用过的密钥会加密保留在对应工作区的历史中，用于继续扫描其输出。此操作不会在服务商处撤销密钥。',
  'connections.confirmDisable':
    '停用 {name}？使用它的 Agent 进程将停止，排队的对话轮次需重新启用后才能运行。',
  'connections.confirm': '确认',
  'connections.editHint':
    '更改地址、密钥或 API 会使使用此连接的 Agent 进程重新启动。移除模型后，新对话轮次不能再使用它。',
  'connections.check': '测试模型',
  'connections.checkHint': '使用所选模型及 API 测试流式工具调用和回复，服务商可能对此计费。',
  'connections.checkModel': '测试模型',
  'connections.checkProtocol': '测试 API',
  'connections.checkPassed': '测试通过',
  'connections.checkFailed': '测试失败',
  'connections.checkStale': '测试期间连接已更改，请重新测试当前配置。',
  'connections.checkScope': '{model} · {protocol}。此测试不能验证其他模型和 API。',
  'connections.discover': '发现模型 ID',
  'connections.discoverHint': '发现功能只获取模型 ID 列表，不验证模型是否能运行 Agent。',
  'connections.discoverEmpty': '服务商未返回模型 ID，仍可手动填写。',
  'connections.addModels': '添加选中的 ID',
  'connections.checkDetails': '流式输出：{streaming} · 工具调用：{tool} · 最终回复：{final}',
  'connections.yes': '通过',
  'connections.no': '未验证',
};
