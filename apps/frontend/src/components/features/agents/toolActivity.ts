// Read task and delegation arguments without deriving outcomes from tool success.
import { EMPTY_TOOL_OUTPUT } from './adapt';

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

/** What a delegating call returned, for calls whose result is the delegated agent's answer. */
export interface DelegationReply {
  /** The call's recorded result; a background launch returns only a notice. */
  state: 'received' | 'background' | 'failed' | 'cancelled' | 'waiting' | 'not_recorded';
  /** The agent status the runtime recorded with the result, verbatim. */
  status?: string;
  /** The returned text, unwrapped from the runtime's content blocks or task envelope. */
  text?: string;
  /** The stored result is incomplete, so the text is not the whole answer. */
  truncated?: boolean;
}

/** One delegating call as recorded, whatever the view that holds it. */
export interface DelegationCall {
  tool: string;
  input: unknown;
  /** The MCP server a Codex call went to; Claude names it in `tool` instead. */
  mcpServer?: string;
  resultState?: 'received' | 'failed' | 'waiting' | 'not_recorded' | 'cancelled' | 'unknown';
  output?: string;
  isError?: boolean;
  structuredOutput?: unknown;
  /** The platform marked the stored result as shortened. */
  outputTruncated?: boolean;
}

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

/**
 * Whether a call went to an MCP server rather than to the runtime itself.
 * Claude Code names such a call `mcp__<server>__<tool>`. Codex keeps the
 * tool's own name and records the server beside it, so its server's `task`
 * reads like Codex's own by name alone. A server may name a tool anything, and
 * none of its tools is the runtime's task list or delegation.
 */
function isMcpCall(toolName: string, mcpServer?: string): boolean {
  return toolName.startsWith('mcp__') || Boolean(mcpServer);
}

/** Unknown tools remain ordinary tool calls; names never imply an outcome. */
export function parseToolActivity(
  toolName: string,
  value: unknown,
  mcpServer?: string,
): ToolActivity | null {
  const key = toolKey(toolName);
  const input = record(value);
  if (
    isMcpCall(toolName, mcpServer) &&
    (['tasks', 'plan'].includes(key) || Object.hasOwn(DELEGATION_TITLES, key))
  )
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
      : typeof input.background === 'boolean'
        ? { background: input.background }
        : {}),
    states: recordedAgentStates(input),
  };
}

// The pinned OpenCode (1.18.9) and Kilo (7.4.17) wrap a task's answer in this
// envelope; the closing tags are missing when the stored result was cut short.
const TASK_ENVELOPE =
  /^<task id="[^"]*" state="(running|completed|error)">\n(?:<summary>[^\n]*<\/summary>\n)?<(task_result|task_error)>\n([\s\S]*?)(\n<\/\2>\n<\/task>)?$/;
const PLATFORM_TRUNCATION = /\n\n\[Content truncated: [^\]\n]*\]$/;

function returnedText(
  output: string,
  marked: boolean,
): { text: string; complete: boolean; envelope?: string } {
  const body = marked ? output.replace(PLATFORM_TRUNCATION, '') : output;
  // Claude records the answer as content blocks; a block array that still
  // parses was not cut, even when another field of its event was.
  const blocks = valueOf(body);
  if (Array.isArray(blocks) && blocks.every((block) => typeof record(block).type === 'string')) {
    const parts = blocks.flatMap((block) => {
      const item = record(block);
      return item.type === 'text' && typeof item.text === 'string' ? [item.text] : [];
    });
    return { text: parts.join('\n\n'), complete: true };
  }
  const envelope = TASK_ENVELOPE.exec(body);
  if (envelope) {
    return { text: envelope[3], complete: envelope[4] !== undefined, envelope: envelope[1] };
  }
  return { text: body, complete: !marked };
}

/**
 * Read what a delegating call returned to the main agent.
 *
 * Only Claude Code's Agent/Task and OpenCode's or Kilo's task return the
 * agent's answer as their own result; other delegation tools return none.
 */
export function recordedDelegationReply(call: DelegationCall): DelegationReply | undefined {
  if (
    !['agent', 'task'].includes(toolKey(call.tool)) ||
    isMcpCall(call.tool, call.mcpServer) ||
    call.resultState === 'unknown'
  ) {
    return undefined;
  }
  if (call.resultState === 'waiting' || call.resultState === 'not_recorded') {
    return { state: call.resultState };
  }
  const failed = call.isError === true || call.resultState === 'failed';
  const cancelled = call.resultState === 'cancelled';
  if (call.output === undefined && !failed && !cancelled) return { state: 'not_recorded' };
  const status = text(record(call.structuredOutput).status);
  const returned =
    call.output === undefined || call.output === EMPTY_TOOL_OUTPUT
      ? { text: '', complete: true }
      : returnedText(call.output, call.outputTruncated === true);
  const details = {
    ...(status ? { status } : {}),
    ...(returned.text.trim() ? { text: returned.text } : {}),
    ...(returned.complete ? {} : { truncated: true }),
  };
  if (cancelled) return { state: 'cancelled', ...details };
  if (failed || returned.envelope === 'error') return { state: 'failed', ...details };
  const input = record(call.input);
  if (
    input.run_in_background === true ||
    input.background === true ||
    status === 'async_launched' ||
    returned.envelope === 'running'
  ) {
    return { state: 'background', ...(status ? { status } : {}) };
  }
  return { state: 'received', ...details };
}
