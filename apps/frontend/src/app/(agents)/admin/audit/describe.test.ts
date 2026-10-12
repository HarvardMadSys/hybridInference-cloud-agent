import { describe, expect, it } from 'vitest';

import type { AdminAuditLogEntry, HostSettingDefinition } from '@/lib/api/admin';

import { auditOutcome, describeActor, describeAuditEntry, describeWhen } from './describe';

const TIMEOUT: HostSettingDefinition = {
  key: 'agent_timeout_s',
  env: 'AGENT_TIMEOUT_S',
  label: 'Turn time limit',
  description: '',
  unit: 'seconds',
  default: 3600,
  minimum: 60,
  maximum: 86400,
  scope: 'runner',
};

function entry(action: string, details: Record<string, unknown> | null = null): AdminAuditLogEntry {
  return {
    id: 1,
    action,
    category: null,
    actor_id: 'admin:u-1',
    admin_ip: '203.0.113.9',
    target_user_id: null,
    details,
    success: true,
    created_at: '2026-10-09T12:00:00Z',
  };
}

describe('describeAuditEntry', () => {
  it('describes archive policy changes from snapshot and legacy audit details', () => {
    expect(
      describeAuditEntry(
        entry('agent.sandbox_archive_settings.set', {
          old_settings: { enabled: false, region: 'us-east-1' },
          new_settings: { enabled: true, region: 'us-east-1' },
        }),
      ),
    ).toBe('Enabled sandbox archives');
    expect(
      describeAuditEntry(
        entry('agent.sandbox_archive_settings.set', {
          old_settings: { enabled: true },
          new_settings: { enabled: false },
        }),
      ),
    ).toBe('Disabled sandbox archives');
    expect(
      describeAuditEntry(
        entry('agent.sandbox_archive_settings.set', {
          old_settings: { enabled: true, region: 'us-east-1' },
          new_settings: { enabled: true, region: 'us-west-2' },
        }),
      ),
    ).toBe('Changed sandbox archive settings');
    expect(
      describeAuditEntry(
        entry('agent.sandbox_archive_settings.set', {
          old_enabled: false,
          new_enabled: true,
        }),
      ),
    ).toBe('Enabled sandbox archives');
  });

  it('describes a sandbox stop as a request, before the host confirms the result', () => {
    expect(
      describeAuditEntry(
        entry('agent.sandbox.stop', { sandbox_id: 'sandbox-one', host: 'host-one' }),
      ),
    ).toBe('Requested stopping sandbox sandbox-one on host-one');
  });

  it('describes database reads using their table without exposing filter values', () => {
    expect(describeAuditEntry(entry('agent.database.query', { table: 'agent_jobs' }))).toBe(
      'Queried database table agent_jobs',
    );
    expect(describeAuditEntry(entry('agent.database.query'))).toBe('Queried the database');
    expect(describeAuditEntry(entry('agent.database.pgadmin.open'))).toBe('Opened pgAdmin');
  });

  it('says what a settings change was, in units, and what it replaced', () => {
    const global = entry('agent.host_settings.set', {
      changes: { agent_timeout_s: 7200 },
      old_values: { agent_timeout_s: null },
    });
    expect(describeAuditEntry(global, [TIMEOUT])).toBe(
      'Set Turn time limit to 2 h for all servers (was the deployment default)',
    );
    const cleared = entry('agent.runner_host.set_settings', {
      host: 'rtx6000b',
      changes: { agent_timeout_s: null },
      old_values: { agent_timeout_s: 1800 },
    });
    expect(describeAuditEntry(cleared, [TIMEOUT])).toBe(
      'Reset Turn time limit on rtx6000b to the Settings value (was 30 min)',
    );
    const reset = entry('agent.host_settings.set', {
      changes: { agent_timeout_s: null },
      old_values: { agent_timeout_s: 7200 },
    });
    expect(describeAuditEntry(reset, [TIMEOUT])).toBe(
      'Reset Turn time limit for all servers to the deployment default (was 2 h)',
    );
  });

  it('falls back to the key and a raw number without definitions', () => {
    const change = entry('agent.host_settings.set', {
      changes: { agent_timeout_s: 7200 },
      old_values: { agent_timeout_s: 3600 },
    });
    expect(describeAuditEntry(change)).toBe(
      'Set agent_timeout_s to 7200 for all servers (was 3600)',
    );
  });

  it('names the agent and both versions of a version change', () => {
    expect(
      describeAuditEntry(
        entry('agent.runtime_version.set', {
          runtime: 'claude-code',
          old_version: null,
          new_version: '2.1.5',
        }),
      ),
    ).toBe('Switched new Claude Code sandboxes to 2.1.5 (was the server default)');
    expect(
      describeAuditEntry(
        entry('agent.runtime_version.set', {
          runtime: 'codex',
          old_version: '0.47.0',
          new_version: null,
        }),
      ),
    ).toBe('Put new Codex sandboxes back on the server default (was 0.47.0)');
  });

  it('describes server actions by the server they touched', () => {
    expect(describeAuditEntry(entry('agent.runner_host.drain', { host: 'rtx6000b' }))).toBe(
      'Paused new work on rtx6000b',
    );
    expect(describeAuditEntry(entry('agent.runner_host.undrain', { host: 'rtx6000b' }))).toBe(
      'Resumed new work on rtx6000b',
    );
    expect(
      describeAuditEntry(entry('agent.runner_host.remove', { host: 'gpu-2', force: true })),
    ).toBe('Removed gpu-2 from the pool while it was running work');
    expect(
      describeAuditEntry(
        entry('agent.runner_host.set_slots', { host: 'gpu-2', old_slots: null, new_slots: 6 }),
      ),
    ).toBe('Set gpu-2 to 6 turns at once (was its own)');
    expect(
      describeAuditEntry(
        entry('agent.runner_host.set_active', { old_host: null, new_host: 'gpu-2' }),
      ),
    ).toBe('Sent new sandboxes to gpu-2');
    expect(describeAuditEntry(entry('agent.compute.allocation.policy'))).toBe(
      "Changed a server's lease policy",
    );
    expect(describeAuditEntry(entry('agent.compute.cloudlab_defaults.set'))).toBe(
      'Changed CloudLab defaults',
    );
  });

  it('says when someone read a user’s content, and reads an unknown action', () => {
    expect(describeAuditEntry(entry('agent.run.event_content.read', { job_id: 'ajob_7f' }))).toBe(
      "Viewed a step's content in turn ajob_7f",
    );
    expect(
      describeAuditEntry(entry('agent.profiling.start', { outcome: 'refused', reason: 'busy' })),
    ).toBe('Tried to turn measurements on (refused: busy)');
    expect(describeAuditEntry(entry('agent.something_new.happened'))).toBe(
      'Something new happened',
    );
  });
});

describe('requests and refusals', () => {
  it('reads a queued server operation as a request, not as its result', () => {
    // These are recorded when the route answers 202; the provider has not
    // acted yet and the operation can still fail.
    const queued: Record<string, string> = {
      'agent.compute.allocation.extend': 'Requested a lease extension',
      'agent.compute.allocation.retire': 'Requested releasing a server',
      'agent.compute.allocation.import': 'Requested importing an existing server',
      'agent.compute.server.add': 'Requested a new server',
      'agent.compute.server.preflight': 'Requested a check of a server before adding it',
      'agent.compute.operation.retry': 'Requested a retry of a server operation',
      'agent.compute.operation.cancel': 'Requested cancelling a server operation',
      'agent.compute.operation.resolve': 'Requested resolving a server operation by hand',
    };
    for (const [action, sentence] of Object.entries(queued)) {
      expect(describeAuditEntry(entry(action))).toBe(sentence);
    }
  });

  it('never reads a refused Stop as measurements turned off', () => {
    const refused = entry('agent.profiling.stop', {
      outcome: 'refused',
      reason: 'run_already_finished',
      run_id: 'admin-1',
    });
    expect(describeAuditEntry(refused)).toBe(
      'Tried to turn measurements off (refused: run_already_finished)',
    );
    // Rows written before refusals were recorded as unsuccessful still say
    // success: true; the outcome in the details decides.
    expect(auditOutcome(refused)).toBe('refused');
    expect(auditOutcome({ ...refused, success: false })).toBe('refused');
    const applied = entry('agent.profiling.stop', { outcome: 'applied', run_id: 'admin-1' });
    expect(describeAuditEntry(applied)).toBe('Turned measurements off');
    expect(auditOutcome(applied)).toBe('done');
    expect(auditOutcome({ ...applied, success: false })).toBe('failed');
  });

  it('reads a replayed Start or Stop as a repeat that changed nothing', () => {
    // A retried request id, or a Stop with nothing running: the control did
    // not move, and an experiment started since may still be running.
    const stop = entry('agent.profiling.stop', { outcome: 'replayed', run_id: 'admin-1' });
    const start = entry('agent.profiling.start', { outcome: 'replayed', run_id: 'admin-2' });
    expect(describeAuditEntry(stop)).toBe('Asked to turn measurements off again; nothing changed');
    expect(describeAuditEntry(start)).toBe('Asked to turn measurements on again; nothing changed');
    expect(auditOutcome(stop)).toBe('replayed');
    expect(auditOutcome(start)).toBe('replayed');
  });
});

describe('describeActor', () => {
  it('calls the signed-in admin "You" and shortens anyone else', () => {
    expect(describeActor('admin:01KQHVXBB167G0EJ', '01KQHVXBB167G0EJ').label).toBe('You');
    expect(describeActor('admin:01KQGM0E6VEG598F', '01KQHVXBB167G0EJ')).toEqual({
      label: '01KQGM0E…',
      title: '01KQGM0E6VEG598F',
    });
    expect(describeActor('admin-token', 'u-1').label).toBe('Admin token');
    expect(describeActor(null, 'u-1').label).toBe('An admin');
  });
});

describe('describeWhen', () => {
  it('is relative for the last day, then a date', () => {
    const now = Date.parse('2026-10-09T12:00:00Z');
    expect(describeWhen('2026-10-09T11:59:30Z', now)).toBe('Just now');
    expect(describeWhen('2026-10-09T11:58:00Z', now)).toBe('2 min ago');
    expect(describeWhen('2026-10-09T09:00:00Z', now)).toBe('3 h ago');
    expect(describeWhen('2026-10-08T09:00:00Z', now)).toBe('Yesterday');
    expect(describeWhen('2026-10-01T09:00:00Z', now)).not.toMatch(/ago/);
  });
});
