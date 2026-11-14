// How a model's source reads wherever a model is chosen or accounted for:
// the composers, the default-model card and a turn's usage line.

export const modelSourcesEn = {
  'modelSource.setupTitle': 'Connect your model to start a task',
  'modelSource.setupDescription':
    'Add the Base URL, API key and model ID from FreeInference or another provider. Your provider manages billing and usage limits; this account has no shared model allowance.',
  'modelSource.setupDescriptionProvided':
    '{name}, which this deployment provides, has no model this account can use right now. To start a task, add a connection of your own: the Base URL, API key and model ID from your provider, which manages its billing and usage limits.',
  'modelSource.setupAction': 'Set up a model connection',
  'modelSource.disabledTitle': 'Model connections are unavailable',
  'modelSource.disabledDescription':
    'The operator has disabled personal model connections for this deployment. Contact them to enable access before starting a task.',
  'modelSource.personal': '{model} · {name}',
  'modelSource.personalUnknown': '{model} · personal connection',
  'modelSource.providedUnknown': '{model} · provided by this deployment',
  'modelSource.providedGroup': 'Provided by this deployment',
  'modelSource.ownGroup': 'Your connections',
  'modelSource.remaining': '{count} left today',
  'modelSource.resets': 'No requests left today · resets at {time}',
  'modelSource.usedUp': '{label} (used up today)',
  'modelSource.usedUpHint':
    '{model} has no requests left today on this deployment. The allowance resets at {time}. Choose another model to continue.',
  'modelSource.unavailable': '{label} (unavailable)',
  'modelSource.unavailableHint':
    '{label} cannot run with {runtime}, or its connection no longer offers it. Choose a model to continue.',
  'modelSource.inheritedUnavailable':
    'This chat runs on {label}, which is unavailable. Choose a model for this turn.',
  'modelSource.usageExternal':
    'Ran on your own connection: your provider bills it, and its cost is not tracked here.',
  'modelSource.usageProvided':
    'Ran on a model this deployment provides: it counts against your daily request allowance, and its cost is not tracked here.',
  'modelSource.steerQueued':
    'A different model is selected, so this message runs as the next turn instead of joining the running one.',
  'modelSource.sendNowMismatch':
    'This message uses a different model or source than the running turn, so it runs as its own next turn.',
} as const;

export const modelSourcesZhCN: Record<keyof typeof modelSourcesEn, string> = {
  'modelSource.setupTitle': '连接自己的模型后即可开始任务',
  'modelSource.setupDescription':
    '填写 FreeInference 或其他服务商的 Base URL、API key 和模型 ID。费用和使用额度由服务商管理，此账号不提供共享模型额度。',
  'modelSource.setupDescriptionProvided':
    '本部署提供的 {name} 目前没有此账号可用的模型。要开始任务，请添加自己的连接：填写服务商的 Base URL、API key 和模型 ID，费用和使用额度由服务商管理。',
  'modelSource.setupAction': '设置模型连接',
  'modelSource.disabledTitle': '模型连接暂不可用',
  'modelSource.disabledDescription':
    '运营方已关闭此部署的个人模型连接功能。请联系运营方开启后再开始任务。',
  'modelSource.personal': '{model} · {name}',
  'modelSource.personalUnknown': '{model} · 个人连接',
  'modelSource.providedUnknown': '{model} · 由本部署提供',
  'modelSource.providedGroup': '由本部署提供',
  'modelSource.ownGroup': '你的连接',
  'modelSource.remaining': '今天还剩 {count} 次',
  'modelSource.resets': '今天的次数已用完 · {time} 重置',
  'modelSource.usedUp': '{label}（今日已用完）',
  'modelSource.usedUpHint':
    '{model} 今天在本部署的请求次数已用完，将在 {time} 重置。请选择其他模型后继续。',
  'modelSource.unavailable': '{label}（不可用）',
  'modelSource.unavailableHint':
    '{label} 无法与 {runtime} 一起运行，或其连接已不再提供该模型。请选择模型后继续。',
  'modelSource.inheritedUnavailable': '此会话使用的 {label} 当前不可用，请为本轮选择模型。',
  'modelSource.usageExternal': '使用你自己的连接运行：费用由服务商计费，此处不统计其费用。',
  'modelSource.usageProvided': '使用本部署提供的模型运行：计入你的每日请求次数，此处不统计其费用。',
  'modelSource.steerQueued': '已选择其他模型，此消息将作为下一轮运行，不会加入正在运行的这一轮。',
  'modelSource.sendNowMismatch':
    '此消息使用的模型或来源与正在运行的这一轮不同，将作为单独的下一轮运行。',
};
