import { describe, expect, it } from 'vitest';

import type { AgentJobApi, AgentJobEventApi } from '@/lib/api/agents';
import { toDisplayJob } from './adapt';

import {
  applyAgentLifecycleEvent,
  hasActiveWorkspacePublication,
  isActiveWorkspacePublicationState,
} from './useAgentJobs';

const JOB = {
  state: 'queued',
  current_attempt_id: 10,
} as AgentJobApi;

function lifecycle(attemptId: number, phase: string): AgentJobEventApi {
  return {
    id: attemptId,
    attempt_id: attemptId,
    seq: 1,
    event_type: 'lifecycle',
    payload: { phase },
    created_at: null,
  };
}

function superseded(attemptId: number): AgentJobEventApi {
  return {
    id: attemptId,
    attempt_id: attemptId,
    seq: 2,
    event_type: 'attempt_superseded',
    payload: { attempt_no: 1, reason: 'lease_expired' },
    created_at: null,
  };
}

describe('applyAgentLifecycleEvent', () => {
  it('keeps legacy telemetry on the fetched attempt across supersede and replacement events', () => {
    const snapshot = {
      ...JOB,
      task_prompt: 'test',
      duration_ms: 3_000,
      context_status: 'lost' as const,
    };
    const requeued = applyAgentLifecycleEvent(snapshot, superseded(10));
    const replacement = applyAgentLifecycleEvent(requeued, lifecycle(11, 'started'));
    expect(replacement.telemetry_attempt_id).toBe(10);
    const display = toDisplayJob(replacement, {
      events: [superseded(10), lifecycle(11, 'started')],
    });
    expect(display.attempts[0].telemetry?.durationMs).toBe(3_000);
    expect(display.attempts[1].telemetry).toBeUndefined();
  });

  it('moves the cached job to the replacement attempt before workspace readiness', () => {
    const started = applyAgentLifecycleEvent(JOB, lifecycle(11, 'started'));

    expect(started.state).toBe('running');
    expect(started.current_attempt_id).toBe(11);

    const ready = applyAgentLifecycleEvent(started, lifecycle(11, 'workspace_ready'));
    expect(ready.current_attempt_id).toBe(11);

    const finalizing = applyAgentLifecycleEvent(ready, lifecycle(11, 'workspace_finalizing'));
    expect(finalizing.state).toBe('running');
    expect(finalizing.current_attempt_id).toBe(11);
  });

  it('invalidates cached readiness as soon as an attempt is superseded', () => {
    const ready = applyAgentLifecycleEvent(JOB, lifecycle(10, 'workspace_ready'));
    const requeued = applyAgentLifecycleEvent(ready, superseded(10));

    expect(requeued.state).toBe('queued');
    expect(requeued.current_attempt_id).toBeNull();

    const replacement = applyAgentLifecycleEvent(ready, lifecycle(11, 'workspace_ready'));
    expect(applyAgentLifecycleEvent(replacement, superseded(10))).toBe(replacement);
  });
});

describe('hasActiveWorkspacePublication', () => {
  it.each(['pending', 'capturing', 'captured', 'publishing'] as const)(
    'keeps a shared terminal job fresh while its snapshot is %s',
    (workspace_publication_state) => {
      expect(
        hasActiveWorkspacePublication({
          ...JOB,
          state: 'succeeded',
          workspace_shared: true,
          workspace_publication_state,
        }),
      ).toBe(true);
    },
  );

  it.each(['published', 'failed', null] as const)(
    'stops refreshing once shared publication is %s',
    (workspace_publication_state) => {
      expect(
        hasActiveWorkspacePublication({
          ...JOB,
          state: 'succeeded',
          workspace_shared: true,
          workspace_publication_state,
        }),
      ).toBe(false);
    },
  );

  it('does not add publication polling to the legacy job path', () => {
    expect(
      hasActiveWorkspacePublication({
        ...JOB,
        workspace_shared: false,
        workspace_publication_state: 'pending',
      }),
    ).toBe(false);
  });

  it('uses the same active-state rule for detail and sidebar refreshes', () => {
    expect(isActiveWorkspacePublicationState('capturing')).toBe(true);
    expect(isActiveWorkspacePublicationState('published')).toBe(false);
    expect(isActiveWorkspacePublicationState(undefined)).toBe(false);
  });
});
