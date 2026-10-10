import {
  parseToolActivity,
  recordedAgentStates,
  recordedAgentTargets,
  recordedTasks,
  type ActivityTask,
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
}

export interface ActivityOverview {
  taskActivity?: TaskActivity;
  subagents: OverviewSubagent[];
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

function taskKey(task: ActivityTask, event: ToolUseEvent, index: number): string {
  return JSON.stringify([event.attemptNo ?? null, task.id ?? null, task.id ? null : index]);
}

function toolKey(event: ToolUseEvent): string {
  return event.tool.split(/\.|__/).at(-1)?.toLowerCase() ?? '';
}

function isLaunch(event: ToolUseEvent): boolean {
  return ['agent', 'task', 'spawn_agent'].includes(toolKey(event));
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
    if (event.kind !== 'tool_use' || !accepted(event)) continue;
    const activity = parseToolActivity(event.tool, event.input);
    if (!activity) continue;
    const output =
      event.resultState === 'waiting' || event.resultState === 'not_recorded'
        ? undefined
        : event.output?.join('\n');
    const key = toolKey(event);

    if (activity.kind === 'tasks') {
      // Child checklists describe delegated work, not the primary agent's plan.
      if (event.parentToolUseId) continue;
      const full = !['taskcreate', 'taskupdate', 'taskget'].includes(key);
      if (full) {
        const snapshot = taskSnapshot(output) ?? taskSnapshot(event.input);
        if (!snapshot) continue;
        tasks.clear();
        for (const [rowIndex, task] of recordedTasks(snapshot).entries()) {
          if (task.status !== 'deleted') tasks.set(taskKey(task, event, rowIndex), task);
        }
        taskActivity = {
          ...activity,
          title: activity.title === 'Plan' ? 'Plan' : 'Tasks',
          tasks: [...tasks.values()],
        };
        continue;
      }

      const inputRows = taskRows(event.input);
      const outputRows = taskRows(output);
      // A create request can lack an ID that its structured result supplies.
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
      taskActivity = { ...taskActivity, kind: 'tasks', title: 'Tasks', tasks: [...tasks.values()] };
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
    if (!targets.length && !isLaunch(event)) continue;
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
      subagents.set(id, {
        key: id,
        activity: merged,
        sourceEvent: firstLaunch ? event : (previous?.sourceEvent ?? event),
        ...(previous ? { lastEvent: event } : {}),
      });
    }
  }

  return { ...(taskActivity ? { taskActivity } : {}), subagents: [...subagents.values()] };
}
