import { describe, expect, it } from 'vitest';

import type { AdminAuditLogEntry, HostSettingDefinition } from '@/lib/api/admin';

import { describeActor, describeAuditEntry, describeWhen } from './describe';

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
  it('says what a settings change was, in units, and what it replaced', () => {
    const global = entry('agent.host_settings.set', {
      changes: { agent_timeout_s: 7200 },
      old_values: { agent_timeout_s: null },
    });
    expect(describeAuditEntry(global, [TIMEOUT])).toBe(
      "Set Turn time limit to 2 h for all servers (was each server's default)",
    );
    const cleared = entry('agent.runner_host.set_settings', {
      host: 'rtx6000b',
      changes: { agent_timeout_s: null },
      old_values: { agent_timeout_s: 1800 },
    });
    expect(describeAuditEntry(cleared, [TIMEOUT])).toBe(
      'Reset Turn time limit on rtx6000b to the Settings value (was 30 min)',
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
    expect(describeAuditEntry(entry('agent.compute.allocation.extend'))).toBe(
      "Extended a server's lease",
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
