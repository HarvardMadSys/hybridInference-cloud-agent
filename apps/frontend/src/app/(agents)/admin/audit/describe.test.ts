import { describe, expect, it } from 'vitest';

import type { AdminAuditLogEntry, HostSettingDefinition } from '@/lib/api/admin';

import actions from './actions.json';
import {
  accountName,
  auditOutcome,
  describeActor,
  describeAuditEntry,
  describeResult,
  describeWhen,
  hasSentence,
} from './describe';

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

function entry(
  action: string,
  details: Record<string, unknown> | null = null,
  over: Partial<AdminAuditLogEntry> = {},
): AdminAuditLogEntry {
  return {
    id: 1,
    action,
    category: null,
    kind: 'change',
    actor_id: 'admin:u-1',
    actor_type: 'admin',
    actor_account_id: 'u-1',
    actor_label: 'juncheng',
    actor_deleted: false,
    admin_ip: '203.0.113.9',
    target_user_id: null,
    subject_label: null,
    subject_deleted: false,
    target_type: 'host',
    target_id: 'h',
    target_label: null,
    details,
    success: true,
    outcome: 'applied',
    result: null,
    result_at: null,
    reason: null,
    created_at: '2026-10-09T12:00:00Z',
    ...over,
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
    expect(auditOutcome({ ...applied, outcome: 'failed' })).toBe('failed');
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
  it('calls the signed-in admin "You" and names anyone else with a short id', () => {
    const own = entry('agent.runner_host.drain', null, { actor_account_id: '01KQHVXBB167G0EJ' });
    expect(describeActor(own, '01KQHVXBB167G0EJ').label).toBe('You');
    const other = entry('agent.runner_host.drain', null, {
      actor_account_id: '01KQGM0E6VEG598F',
      actor_label: 'juncheng',
    });
    expect(describeActor(other, '01KQHVXBB167G0EJ')).toEqual({
      label: 'juncheng (01KQGM0E…)',
      title: '01KQGM0E6VEG598F',
    });
    const gone = { ...other, actor_label: null, actor_deleted: true };
    expect(describeActor(gone, null).label).toBe('01KQGM0E…, since deleted');
  });

  it('marks access that belongs to no account, and quiets automation', () => {
    const token = entry('agent.runner_host.drain', null, {
      actor_id: 'admin-token',
      actor_type: 'break_glass',
      actor_account_id: null,
    });
    expect(describeActor(token, 'u-1')).toMatchObject({ label: 'Admin token', tone: 'alert' });
    expect(describeActor({ ...token, actor_id: 'operator:realtmxi' }, 'u-1').label).toBe(
      'Operator realtmxi',
    );
    expect(
      describeActor(
        { ...token, actor_id: 'service:deploy-pipeline', actor_type: 'service' },
        'u-1',
      ),
    ).toMatchObject({ label: 'deploy-pipeline', tone: 'quiet' });
    expect(
      describeActor({ ...token, actor_id: 'signup-policy', actor_type: 'system' }, 'u-1').label,
    ).toBe('Sign-up policy');
    expect(
      describeActor({ ...token, actor_id: 'anonymous', actor_type: 'anonymous' }, 'u-1').label,
    ).toBe('Not signed in');
  });
});

describe('accounts', () => {
  const about = {
    target_type: 'account',
    target_id: '01M6DMPERW045CW6',
    target_user_id: '01M6DMPERW045CW6',
    subject_label: 'alice@example.com',
  };

  it('says which account an administrator decided about', () => {
    expect(describeAuditEntry(entry('agent.local_account.approve', {}, about))).toBe(
      'Approved alice@example.com (01M6DMPE…)',
    );
    expect(describeAuditEntry(entry('agent.local_account.grant_admin', {}, about))).toBe(
      'Made alice@example.com (01M6DMPE…) an administrator',
    );
    expect(
      describeAuditEntry(
        entry('agent.local_account.erase', {}, { ...about, subject_deleted: true }),
      ),
    ).toBe('Erased alice@example.com (01M6DMPE…) and all of its data');
    expect(describeAuditEntry(entry('agent.local_account.signup', {}, about))).toBe(
      'alice@example.com (01M6DMPE…) signed up',
    );
  });

  it('names an account by its id when no name was recorded', () => {
    expect(accountName('01M6DMPERW045CW6', null)).toBe('01M6DMPE…');
    expect(accountName(null, null)).toBe('an account');
  });

  it('says whose sandbox an administrator acted on', () => {
    expect(
      describeAuditEntry(
        entry(
          'agent.sandbox.delete.requested',
          { sandbox_id: 'asbx_1' },
          {
            target_user_id: 'owner-1',
            subject_label: 'bob',
          },
        ),
      ),
    ).toBe("Requested deleting bob (owner-1)'s sandbox asbx_1");
  });

  it('describes sign-up domains and mail to accounts', () => {
    expect(
      describeAuditEntry(
        entry('agent.local_signup_domain.add', { domain: 'harvard.edu', wildcard: true }),
      ),
    ).toBe('Allowed sign-up from *.harvard.edu');
    expect(describeAuditEntry(entry('agent.local_broadcast.create', { recipients: 1 }))).toBe(
      'Sent mail to 1 account',
    );
  });
});

describe('describeResult', () => {
  it('says what came of a request recorded before it ran', () => {
    const requested = entry('agent.compute.server.add', {}, { outcome: 'requested' });
    expect(describeResult(requested)).toBe('Still in progress');
    expect(
      describeResult({ ...requested, result: 'applied', result_at: '2026-10-09T12:02:30Z' }),
    ).toBe('Completed 2 min later');
    expect(
      describeResult({ ...requested, result: 'failed', result_at: '2026-10-09T15:00:00Z' }),
    ).toBe('Failed 3 h later');
    expect(describeResult({ ...requested, result: 'cancelled' })).toBe('Cancelled');
    expect(describeResult(entry('agent.runner_host.drain'))).toBeNull();
  });
});

describe('every declared action', () => {
  it('has a sentence of its own on this page', () => {
    // actions.json is the backend's registry, checked against it by
    // tests/unit/test_audit_registry.py.
    const missing = actions.map((item) => item.action).filter((action) => !hasSentence(action));
    expect(missing).toEqual([]);
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

  it('counts days in UTC, as every admin page does, so Yesterday is only the day before', () => {
    const now = Date.parse('2026-10-09T12:00:00Z');
    expect(describeWhen('2026-10-08T00:30:00Z', now)).toBe('Yesterday');
    // Under two days old, but two days back.
    expect(describeWhen('2026-10-07T13:00:00Z', now)).toBe('Oct 7');
    expect(describeWhen('2026-10-07T23:30:00Z', Date.parse('2026-10-09T00:30:00Z'))).toBe('Oct 7');
  });
});
