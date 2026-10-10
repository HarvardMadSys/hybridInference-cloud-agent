import type { AgentJobApi, AgentJobEventApi } from '@/lib/api/agents';
import { projectProtocolEvents, rawNotification, record } from './trajectoryProtocol';

/** Infer current activity from the live attempt's recorded runtime boundaries. */
export function agentLiveNote(
  job: Pick<AgentJobApi, 'id' | 'state' | 'current_attempt_id'>,
  events: AgentJobEventApi[],
): string | undefined {
  if (job.state !== 'running' || job.current_attempt_id == null) return undefined;

  const current = events.filter(
    (event) =>
      event.attempt_id === job.current_attempt_id &&
      (!('job_id' in event) || event.job_id === job.id),
  );
  const pendingTools = new Set<string>();
  let response: 'waiting' | 'streaming' | undefined;
  let settled = false;

  for (const event of projectProtocolEvents(current)) {
    // Cancellation drains the runtime stream; those late records cannot
    // make a stopped turn appear active again.
    if (settled) continue;
    const payload = record(event.payload);
    if (event.event_type === 'tool_use') {
      const key =
        typeof payload.id === 'string' && payload.id ? `id:${payload.id}` : `event:${event.id}`;
      // Some runtimes record the finished call with its result inline.
      const finished =
        typeof payload.exit_code === 'number' ||
        ['completed', 'failed', 'cancelled'].includes(String(payload.native_tool_status)) ||
        ['completed', 'failed', 'error', 'success', 'succeeded'].includes(String(payload.status));
      if (finished) pendingTools.delete(key);
      else pendingTools.add(key);
      response = 'waiting';
    } else if (event.event_type === 'tool_result') {
      if (typeof payload.tool_use_id === 'string' && payload.tool_use_id) {
        pendingTools.delete(`id:${payload.tool_use_id}`);
      } else if (pendingTools.size === 1) {
        // An id-less legacy result can settle only an unambiguous single call.
        pendingTools.clear();
      }
      response = 'waiting';
    } else if (event.event_type === 'message') {
      response = ['started', 'delta', 'snapshot', 'reset'].includes(String(payload.stream_state))
        ? 'streaming'
        : 'waiting';
    } else if (event.event_type === 'thinking') {
      response = 'waiting';
    } else if (event.event_type === 'lifecycle') {
      const phase = String(payload.phase);
      if (phase === 'turn_started' || phase === 'init' || phase === 'system') response = 'waiting';
      if (
        [
          'result',
          'cancelled_by_owner',
          'workspace_finalizing',
          'shared_workspace_settled',
          'publishing',
          'shared_snapshot_publishing',
        ].includes(phase)
      ) {
        pendingTools.clear();
        response = undefined;
        settled = true;
      }
    } else if (event.event_type === 'raw') {
      const notification = rawNotification(event);
      if (notification?.method === 'turn/started') response = 'waiting';
      if (notification?.method === 'turn/completed') {
        pendingTools.clear();
        response = undefined;
        settled = true;
      }
    } else if (event.event_type === 'attempt_superseded' || event.event_type === 'error') {
      pendingTools.clear();
      response = undefined;
      settled = event.event_type === 'attempt_superseded';
    }
  }

  // A sibling call returning does not end the other parallel tools' activity.
  if (pendingTools.size > 0) return 'Running tool call';
  if (response === 'waiting') return 'Waiting for LLM response';
  if (response === 'streaming') return 'Receiving LLM response';
  return undefined;
}
