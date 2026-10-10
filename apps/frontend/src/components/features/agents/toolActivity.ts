// Read task and delegation arguments without deriving outcomes from tool success.
export interface ActivityTask {
  id?: string;
  subject: string;
  status?: string;
  description?: string;
  owner?: string;
  blockedBy: string[];
  blocks: string[];
}

export interface ActivityAgentState {
  id: string;
  status: string;
  message?: string;
}

export type ToolActivity =
  | {
      kind: 'tasks';
      title: string;
      tasks: ActivityTask[];
      explanation?: string;
    }
  | {
      kind: 'delegation';
      title: string;
      description?: string;
      agentType?: string;
      model?: string;
      prompt?: string;
      promptLabel: 'Prompt' | 'Message';
      targets: string[];
      background?: boolean;
      states: ActivityAgentState[];
    };

function valueOf(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  try {
    return JSON.parse(value);
  } catch {
    return null;
  }
}

function record(value: unknown): Record<string, unknown> {
  const parsed = valueOf(value);
  return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
    ? (parsed as Record<string, unknown>)
    : {};
}

function text(...values: unknown[]): string | undefined {
  return values.find((value): value is string => typeof value === 'string' && !!value.trim());
}

function identifier(value: unknown): string | undefined {
  return (
    text(value) ?? (typeof value === 'number' && Number.isFinite(value) ? String(value) : undefined)
  );
}

function identifiers(value: unknown): string[] {
  const items = Array.isArray(value) ? value : [value];
  return [...new Set(items.flatMap((item) => identifier(item) ?? []))];
}

export function recordedAgentTargets(value: unknown): string[] {
  const input = record(value);
  return identifiers(
    input.receiver_thread_ids ?? input.receiverThreadIds ?? input.agent_id ?? input.thread_id,
  );
}

/** Task lists are accepted only as recorded structured data. */
export function recordedTasks(value: unknown): ActivityTask[] {
  const parsed = valueOf(value);
  const input = record(parsed);
  const rows = Array.isArray(parsed) ? parsed : (input.tasks ?? input.plan ?? input.todos);
  if (!Array.isArray(rows)) return [];
  return rows.flatMap((row) => {
    const task = record(row);
    const id = identifier(task.id ?? task.taskId ?? task.task_id);
    const subject = text(task.subject, task.step, task.content, task.title);
    if (!subject && !id) return [];
    return [
      {
        ...(id ? { id } : {}),
        subject: subject ?? `Task #${id}`,
        status: text(task.status),
        description: text(task.description),
        owner: text(task.owner),
        blockedBy: identifiers(task.blockedBy ?? task.blocked_by ?? task.addBlockedBy),
        blocks: identifiers(task.blocks ?? task.addBlocks),
      },
    ];
  });
}

/** A status is shown only when the runtime explicitly recorded an agent state. */
export function recordedAgentStates(value: unknown): ActivityAgentState[] {
  const input = record(value);
  const states = input.agents_states ?? input.agentsStates ?? input.status;
  if (!states || typeof states !== 'object' || Array.isArray(states)) return [];
  return Object.entries(states).flatMap(([id, state]) => {
    const details = record(state);
    let status = text(typeof state === 'string' ? state : undefined, details.status);
    let message = text(details.message);
    // Codex wait results can encode a state as {completed: "the answer"}.
    if (!status) {
      const stateKey = [
        'pending_init',
        'pendingInit',
        'running',
        'completed',
        'errored',
        'shutdown',
        'not_found',
        'notFound',
      ].find((key) => Object.prototype.hasOwnProperty.call(details, key));
      if (stateKey) {
        status = stateKey;
        message = text(details[stateKey]);
      }
    }
    return status ? [{ id, status, ...(message ? { message } : {}) }] : [];
  });
}

function toolKey(name: string): string {
  return name.split(/\.|__/).at(-1)?.toLowerCase() ?? '';
}

const DELEGATION_TITLES: Record<string, string> = {
  task: 'Delegate task',
  agent: 'Delegate task',
  spawn_agent: 'Start subagent',
  send_message: 'Message subagent',
  send_input: 'Message subagent',
  wait: 'Wait for subagents',
  close_agent: 'Close subagent',
  resume_agent: 'Resume subagent',
  followup_task: 'Follow up with subagent',
  interrupt_agent: 'Interrupt subagent',
  list_agents: 'List subagents',
};

/** Unknown tools remain ordinary tool calls; names never imply an outcome. */
export function parseToolActivity(toolName: string, value: unknown): ToolActivity | null {
  const key = toolKey(toolName);
  const input = record(value);
  if (toolName.startsWith('mcp__') && ['tasks', 'plan', 'wait', 'send_message'].includes(key))
    return null;
  if (['tasks', 'plan'].includes(key) && !Array.isArray(input.tasks ?? input.plan ?? input.todos))
    return null;
  if (['todowrite', 'update_plan', 'write_todos', 'tasks', 'plan'].includes(key)) {
    return {
      kind: 'tasks',
      title: key === 'update_plan' || key === 'plan' ? 'Plan' : 'Tasks',
      tasks: recordedTasks(value),
      explanation: text(input.explanation),
    };
  }
  if (['taskcreate', 'taskupdate', 'tasklist', 'taskget'].includes(key)) {
    const titles: Record<string, string> = {
      taskcreate: 'Create task',
      taskupdate: 'Update task',
      tasklist: 'Task list',
      taskget: 'Task details',
    };
    return {
      kind: 'tasks',
      title: titles[key],
      tasks: key === 'tasklist' ? recordedTasks(value) : recordedTasks({ tasks: [input] }),
    };
  }
  const title = Object.hasOwn(DELEGATION_TITLES, key) ? DELEGATION_TITLES[key] : undefined;
  if (!title) return null;
  const targets = identifiers(
    input.receiver_thread_ids ??
      input.receiverThreadIds ??
      input.ids ??
      input.id ??
      input.target ??
      input.agent_id ??
      input.resume,
  );
  if (key === 'wait' && (targets.length === 0 || 'cell_id' in input || 'session_id' in input))
    return null;
  if (
    key === 'send_message' &&
    !input.target &&
    !input.agent_id &&
    !input.receiver_thread_ids &&
    !input.receiverThreadIds
  )
    return null;
  return {
    kind: 'delegation',
    title,
    description: text(input.description, input.task_name, input.name),
    agentType: text(input.subagent_type, input.agent_type, input.agentType),
    model: text(input.model),
    prompt: text(input.prompt, input.message),
    promptLabel: key === 'send_message' || key === 'send_input' ? 'Message' : 'Prompt',
    targets,
    ...(typeof input.run_in_background === 'boolean'
      ? { background: input.run_in_background }
      : {}),
    states: recordedAgentStates(input),
  };
}
