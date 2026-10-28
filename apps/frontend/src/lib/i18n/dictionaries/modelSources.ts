// How a model's source reads wherever a model is chosen or accounted for:
// the composers, the default-model card and a turn's usage line.

export const modelSourcesEn = {
  'modelSource.personal': '{model} · {name}',
  'modelSource.personalUnknown': '{model} · personal connection',
  'modelSource.unavailable': '{label} (unavailable)',
  'modelSource.unavailableHint':
    '{label} cannot run with {runtime} right now: it may need verifying in Settings, or its connection no longer offers it. Choose a model to continue.',
  'modelSource.inheritedUnavailable':
    'This chat runs on {label}, which is unavailable. Choose a model for this turn.',
  'modelSource.rewriteUnavailable':
    'Rewrite uses platform inference and is unavailable while a personal connection is selected.',
  'modelSource.usageExternal':
    'Ran on your own connection: your provider bills it, and its cost is not tracked here.',
  'modelSource.steerQueued':
    'A different model is selected, so this message runs as the next turn instead of joining the running one.',
  'modelSource.sendNowMismatch':
    'This message uses a different model or source than the running turn, so it runs as its own next turn.',
  'settings.model.lastPersonal':
    'Your last task ran on {model} through your own connection. New tasks offer it again while that connection is enabled and lists it; a default chosen here uses platform models instead.',
} as const;

export const modelSourcesZhCN: Record<keyof typeof modelSourcesEn, string> = {
  'modelSource.personal': '{model} · {name}',
  'modelSource.personalUnknown': '{model} · 个人连接',
  'modelSource.unavailable': '{label}（不可用）',
  'modelSource.unavailableHint':
    '{label} 当前无法与 {runtime} 一起运行：可能需要先在设置中验证，或其连接已不再提供该模型。请选择模型后继续。',
  'modelSource.inheritedUnavailable': '此会话使用的 {label} 当前不可用，请为本轮选择模型。',
  'modelSource.rewriteUnavailable': '改写使用平台推理，选择个人连接时不可用。',
  'modelSource.usageExternal': '使用你自己的连接运行：费用由服务商计费，此处不统计其费用。',
  'modelSource.steerQueued': '已选择其他模型，此消息将作为下一轮运行，不会加入正在运行的这一轮。',
  'modelSource.sendNowMismatch':
    '此消息使用的模型或来源与正在运行的这一轮不同，将作为单独的下一轮运行。',
  'settings.model.lastPersonal':
    '你上一个任务通过自己的连接使用了 {model}。只要该连接仍启用并列出此模型，新任务会继续提供它；在此选择默认模型则改用平台模型。',
};
