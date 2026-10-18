import {
  parseToolActivity,
  recordedAgentStates,
  recordedAgentTargets,
  recordedDelegationReply,
  recordedTasks,
  type ActivityTask,
  type DelegationReply,
  type ToolActivity,
} from './toolActivity';
import type { AgentEvent } from './types';

type ToolUseEvent = Extract<AgentEvent, { kind: 'tool_use' }>;
type TaskActivity = Extract<ToolActivity, { kind: 'tasks' }>;
type DelegationActivity = Extract<ToolActivity, { kind: 'delegation' }>;

export interface OverviewSubagent {
  key: string;
  activity: DelegationActivity;
  sourceEvent: ToolUseEvent;
  lastEvent?: ToolUseEvent;
  reply?: DelegationReply;
}

export interface ActivityOverview {
  taskActivity?: TaskActivity;
  subagents: OverviewSubagent[];
}

export function hasActivityOverview(overview: ActivityOverview): boolean {
  return Boolean(overview.taskActivity?.tasks.length || overview.subagents.length);
}

function parsed(value: unknown): unknown {
  if (typeof value !== 'string') return value;
  try {
    return JSON.parse(value);
  } catch {
    return undefined;
  }
}

function record(value: unknown): Record<string, unknown> | undefined {
  const result = parsed(value);
  return result && typeof result === 'object' && !Array.isArray(result)
    ? (result as Record<string, unknown>)
    : undefined;
}

function taskSnapshot(value: unknown): unknown[] | undefined {
  const result = parsed(value);
  if (Array.isArray(result)) return result;
  const input = record(result);
  const rows = input?.tasks ?? input?.plan ?? input?.todos;
  return Array.isArray(rows) ? rows : undefined;
}

function taskRows(value: unknown): Record<string, unknown>[] {
  const snapshot = taskSnapshot(value);
  if (snapshot) {
    return snapshot.flatMap((row) => {
      const task = record(row);
      return task && recordedTasks({ tasks: [task] }).length ? [task] : [];
    });
  }
  const input = record(value);
  if (!input) return [];
  const task = record(input.task) ?? input;
  return recordedTasks({ tasks: [task] }).length ? [task] : [];
}

function taskResultText(value: string): string {
  const blocks = parsed(value);
  if (Array.isArray(blocks) && blocks.length === 1) {
    const block = record(blocks[0]);
    if (block?.type === 'text' && typeof block.text === 'string') return block.text;
  }
  return value;
}

function taskKey(task: ActivityTask, event: ToolUseEvent, index: number): string {
  return JSON.stringify([event.attemptNo ?? null, task.id ?? null, task.id ? null : index]);
}

function toolKey(event: ToolUseEvent): string {
  return event.tool.split(/\.|__/).at(-1)?.toLowerCase() ?? '';
}

function isLaunch(event: ToolUseEvent): boolean {
  return ['agent', 'task', 'spawn_agent'].includes(toolKey(event));
}

function replyOf(event: ToolUseEvent): DelegationReply | undefined {
  return recordedDelegationReply({
    tool: event.tool,
    input: event.input,
    resultState: event.resultState,
    output: event.output?.join('\n'),
    isError: event.outputIsError,
    structuredOutput: event.structuredOutput,
    outputTruncated: event.outputTruncated,
  });
}

function mergeTask(previous: ActivityTask | undefined, raw: Record<string, unknown>): ActivityTask {
  const task = recordedTasks({ tasks: [raw] })[0];
  if (!previous) return task;
  const merged = { ...previous };
  if (['subject', 'step', 'content', 'title'].some((field) => typeof raw[field] === 'string')) {
    merged.subject = task.subject;
  }
  for (const field of ['status', 'description', 'owner'] as const) {
    if (typeof raw[field] === 'string' || raw[field] === null) merged[field] = task[field];
  }
  for (const [field, alias, addition] of [
    ['blockedBy', 'blocked_by', 'addBlockedBy'],
    ['blocks', 'blocks', 'addBlocks'],
  ] as const) {
    if (Array.isArray(raw[field]) || Array.isArray(raw[alias])) {
      merged[field] = task[field];
    } else if (Array.isArray(raw[addition])) {
      merged[field] = [...new Set([...previous[field], ...task[field]])];
    }
  }
  return merged;
}

function accepted(event: ToolUseEvent): boolean {
  return (
    !event.outputIsError && !['failed', 'cancelled', 'unknown'].includes(event.resultState ?? '')
  );
}

/** Read only recorded task/agent data from the caller's selected turn and attempt. */
export function buildActivityOverview(events: AgentEvent[]): ActivityOverview {
  let taskActivity: TaskActivity | undefined;
  const tasks = new Map<string, ActivityTask>();
  const subagents = new Map<string, OverviewSubagent>();

  for (const [index, event] of events.entries()) {
    if (event.kind !== 'tool_use') continue;
    const activity = parseToolActivity(event.tool, event.input, event.mcpServer);
    if (!activity) continue;
    if (!accepted(event)) {
      // A launch that failed or was interrupted is still delegated work. It
      // claims no agent identity or state, so it cannot change another row.
      if (activity.kind === 'delegation' && isLaunch(event) && event.resultState !== 'unknown') {
        const id = JSON.stringify([
          event.attemptNo ?? null,
          'call',
          event.activityKey ?? event.sourceEventId ?? index,
        ]);
        subagents.set(id, {
          key: id,
          activity: { ...activity, targets: [], states: [] },
          sourceEvent: event,
          reply: replyOf(event) ?? {
            state: event.resultState === 'cancelled' ? 'cancelled' : 'failed',
          },
        });
      }
      continue;
    }
    const output =
      event.resultState === 'waiting' || event.resultState === 'not_recorded'
        ? undefined
        : event.output?.join('\n');
    const structuredOutput =
      event.resultState === 'waiting' || event.resultState === 'not_recorded'
        ? undefined
        : event.structuredOutput;
    const key = toolKey(event);

    if (activity.kind === 'tasks') {
      // Child checklists describe delegated work, not the primary agent's plan.
      if (event.parentToolUseId) continue;
      const full = !['taskcreate', 'taskupdate', 'taskget'].includes(key);
      if (full) {
        const snapshot =
          taskSnapshot(structuredOutput) ?? taskSnapshot(output) ?? taskSnapshot(event.input);
        if (!snapshot) continue;
        tasks.clear();
        for (const [rowIndex, task] of recordedTasks(snapshot).entries()) {
          if (task.status !== 'deleted') tasks.set(taskKey(task, event, rowIndex), task);
        }
        const currentTasks = [...tasks.values()];
        taskActivity = undefined;
        if (currentTasks.length) {
          taskActivity = {
            ...activity,
            title: activity.title === 'Plan' ? 'Plan' : 'Tasks',
            tasks: currentTasks,
          };
        }
        continue;
      }

      const inputRows = taskRows(event.input);
      const outputRows = taskRows(structuredOutput);
      if (!outputRows.length) outputRows.push(...taskRows(output));
      // Claude's native TaskCreate returns a text acknowledgement, not JSON.
      // Use its explicit ID only when it acknowledges this request's subject.
      if (key === 'taskcreate' && inputRows.length === 1 && !outputRows.length && output) {
        const created = /^Task #([1-9]\d*) created successfully: ([\s\S]+)$/.exec(
          taskResultText(output).trim(),
        );
        const requested = recordedTasks({ tasks: inputRows })[0];
        if (created && !requested.id && created[2].trim() === requested.subject.trim()) {
          outputRows.push({ id: created[1] });
        }
      }
      // Link the create request's details to the ID supplied by its result.
      if (
        key === 'taskcreate' &&
        inputRows.length === 1 &&
        outputRows.length === 1 &&
        !recordedTasks({ tasks: inputRows })[0].id &&
        recordedTasks({ tasks: outputRows })[0].id
      ) {
        outputRows[0] = { ...inputRows[0], ...outputRows[0] };
        inputRows.length = 0;
      }
      const rows = [...inputRows, ...outputRows];
      if (!rows.length) continue;
      // Result acknowledgements often repeat only the ID. Merge one call's
      // patches before applying so an omitted result status cannot undo deletion.
      const patches = new Map<string, Record<string, unknown>>();
      for (const [rowIndex, raw] of rows.entries()) {
        const task = recordedTasks({ tasks: [raw] })[0];
        if (key !== 'taskcreate' && !task.id) continue;
        const callKey = JSON.stringify([
          event.attemptNo ?? null,
          'call',
          event.sourceEventId ?? index,
          rowIndex,
        ]);
        const id = task.id ? taskKey(task, event, rowIndex) : callKey;
        patches.set(id, { ...patches.get(id), ...raw });
      }
      if (!patches.size) continue;
      for (const [id, raw] of patches) {
        const task = recordedTasks({ tasks: [raw] })[0];
        if (task.status === 'deleted') tasks.delete(id);
        else tasks.set(id, mergeTask(tasks.get(id), raw));
      }
      const currentTasks = [...tasks.values()];
      taskActivity = currentTasks.length
        ? { ...taskActivity, kind: 'tasks', title: 'Tasks', tasks: currentTasks }
        : undefined;
      continue;
    }

    const states = [...activity.states, ...recordedAgentStates(output)].filter((state) =>
      Boolean(state.id.trim()),
    );
    const targets = [
      ...new Set([
        ...activity.targets,
        ...recordedAgentTargets(output),
        ...states.map((state) => state.id),
      ]),
    ];
    const hasDetails =
      activity.description !== undefined ||
      activity.agentType !== undefined ||
      activity.model !== undefined ||
      activity.prompt !== undefined ||
      activity.background !== undefined;
    if (!targets.length && !states.length && !hasDetails) continue;
    for (const target of targets.length ? targets : [undefined]) {
      const id = JSON.stringify([
        event.attemptNo ?? null,
        target ? 'agent' : 'call',
        target ?? event.activityKey ?? event.sourceEventId ?? index,
      ]);
      const previous = subagents.get(id);
      const firstLaunch = isLaunch(event) && previous && !isLaunch(previous.sourceEvent);
      const agentStates = new Map(previous?.activity.states.map((state) => [state.id, state]));
      for (const state of states) {
        if (state.id === target) agentStates.set(state.id, state);
      }
      const merged: DelegationActivity = {
        ...(previous?.activity ?? activity),
        ...(firstLaunch ? { title: activity.title } : {}),
        targets: target ? [target] : [],
        states: [...agentStates.values()],
      };
      for (const field of ['description', 'agentType', 'model', 'prompt', 'background'] as const) {
        if (activity[field] !== undefined) Object.assign(merged, { [field]: activity[field] });
      }
      if (activity.prompt !== undefined) merged.promptLabel = activity.promptLabel;
      const reply = replyOf(event) ?? previous?.reply;
      subagents.set(id, {
        key: id,
        activity: merged,
        sourceEvent: firstLaunch ? event : (previous?.sourceEvent ?? event),
        ...(previous ? { lastEvent: event } : {}),
        ...(reply ? { reply } : {}),
      });
    }
  }

  return { ...(taskActivity ? { taskActivity } : {}), subagents: [...subagents.values()] };
}

const overviews = new WeakMap<AgentEvent[], ActivityOverview>();

/**
 * `buildActivityOverview` for a turn's event list, built once per list. A
 * stored turn keeps its list while the newest one streams, so finding the
 * subagent behind an older call does not read that turn again.
 */
export function turnActivityOverview(events: AgentEvent[]): ActivityOverview {
  let overview = overviews.get(events);
  if (!overview) {
    overview = buildActivityOverview(events);
    overviews.set(events, overview);
  }
  return overview;
}

/** The subagent a delegating call started, if that call is the one that started it. */
export function subagentStartedBy(
  overview: ActivityOverview,
  event: ToolUseEvent,
): OverviewSubagent | undefined {
  return overview.subagents.find(
    ({ sourceEvent }) =>
      sourceEvent === event ||
      (sourceEvent.id === event.id &&
        sourceEvent.attemptNo === event.attemptNo &&
        sourceEvent.sourceEventId === event.sourceEventId),
  );
}
