// A conversation's composer: its send, queue and steer actions, the options
// that hold its secondary prompt controls, and the To-dos strip above it.

export const composerEn = {
  'composer.send': 'Send',
  'composer.sendHint': 'Send — Enter to send, Shift+Enter for a new line',
  'composer.queue': 'Queue',
  'composer.queueHint': 'Send after the current turn finishes',
  'composer.steer': 'Steer now',
  'composer.steerHint': 'Send into the current turn — {keys}',
  'composer.options': 'Prompt options',
  'composer.todos': 'To-dos',
  'composer.todosInProgress': '{count} in progress',
  'composer.todosPending': '{count} pending',
  'composer.todosCompleted': '{count} completed',
  'composer.todosCount': '{count} total',
};

export const composerZhCN: Record<keyof typeof composerEn, string> = {
  'composer.send': '发送',
  'composer.sendHint': '发送 — 回车发送，Shift+回车换行',
  'composer.queue': '排队',
  'composer.queueHint': '当前轮次结束后发送',
  'composer.steer': '立即引导',
  'composer.steerHint': '发送到当前轮次 — {keys}',
  'composer.options': '提示词选项',
  'composer.todos': '待办',
  'composer.todosInProgress': '{count} 项进行中',
  'composer.todosPending': '{count} 项待处理',
  'composer.todosCompleted': '{count} 项已完成',
  'composer.todosCount': '共 {count} 项',
};
