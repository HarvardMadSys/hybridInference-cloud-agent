// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentRunnerHost, HostSettingDefinition } from '@/lib/api/admin';

import { GlobalSettingsCard, ServerSettingsPanel } from './HostSettings';

afterEach(cleanup);

function definition(key: string, label = key): HostSettingDefinition {
  return {
    key,
    label,
    env: key.toUpperCase(),
    description: `Description of ${label}.`,
    unit: 'count',
    default: 8,
    minimum: 1,
    maximum: 128,
    scope: 'runner',
  };
}

const definitions = [
  definition('sandbox_cpu_millicores'),
  definition('sandbox_memory_mib'),
  definition('sandbox_disk_mib'),
  definition('agent_timeout_s'),
  definition('source_import_max_mib'),
  definition('chat_idle_timeout_s'),
  definition('shared_resident_capacity'),
  definition('terminal_buffer_max_events'),
  definition('workspace_command_output_max_mib'),
  definition('storage_min_available_mib'),
  definition('future_setting'),
];

const terminalLifetime: HostSettingDefinition = {
  ...definition('terminal_hard_ttl_s', 'Terminal max lifetime'),
  unit: 'seconds',
  default: 28800,
  minimum: 60,
  maximum: 86400,
  scope: 'sandbox_manager',
};

const host: AgentRunnerHost = {
  host: 'runner-a',
  active: false,
  last_worker_id: null,
  first_seen_at: '2026-09-11T12:00:00Z',
  last_seen_at: '2026-09-11T12:00:00Z',
  seconds_since_seen: 1,
  status: 'online',
  drain_fence_epoch: '0',
  enrolled: true,
  labels: {},
  capabilities: ['host-settings-v1'],
  slots_total: 3,
  slots_used: 0,
  runner_version: null,
  sandbox_manager_url: null,
  last_heartbeat_at: null,
  sandbox_runtime: null,
  sandbox_image_digest: null,
  runtime_versions: null,
  runtime_versions_available: null,
  capacity_epoch: null,
  capacity: null,
  settings_desired: { terminal_hard_ttl_s: 28800 },
  settings_applied: { terminal_hard_ttl_s: 28800 },
};

describe('sandbox setting organization', () => {
  it('keeps essential and future settings visible while grouping advanced limits', () => {
    const { container } = render(
      <GlobalSettingsCard
        title="Sandbox defaults"
        definitions={definitions}
        values={{}}
        servers={[]}
        busy={false}
        onChange={vi.fn()}
      />,
    );

    for (const setting of definitions.slice(0, 7)) {
      expect(screen.getByLabelText(`${setting.label} for all servers`)).toBeVisible();
    }
    expect(screen.getByLabelText('future_setting for all servers')).toBeVisible();
    const advanced = container.querySelector('details')!;
    expect(advanced).not.toHaveAttribute('open');
    for (const setting of definitions.slice(7, 10)) {
      expect(within(advanced).getByLabelText(`${setting.label} for all servers`)).not.toBeVisible();
    }

    fireEvent.click(within(advanced).getByText('Advanced'));
    expect(within(advanced).getByRole('heading', { name: 'Terminal limits' })).toBeVisible();
    expect(within(advanced).getByRole('heading', { name: 'Workspace limits' })).toBeVisible();
    expect(within(advanced).getByRole('heading', { name: 'Storage admission' })).toBeVisible();
  });

  it.each(['global', 'server'] as const)(
    'preserves a %s draft and its unit when Advanced is closed and polling rerenders',
    (scope) => {
      const onChange = vi.fn();
      const view = (secondsSinceSeen: number) =>
        scope === 'global' ? (
          <GlobalSettingsCard
            title="Sandbox defaults"
            definitions={[terminalLifetime]}
            values={{ terminal_hard_ttl_s: 28800 }}
            servers={[
              { name: 'runner-a', host: { ...host, seconds_since_seen: secondsSinceSeen } },
            ]}
            busy={false}
            onChange={onChange}
          />
        ) : (
          <ServerSettingsPanel
            name="runner-a"
            host={{ ...host, seconds_since_seen: secondsSinceSeen }}
            definitions={[terminalLifetime]}
            globalValues={{}}
            busy={false}
            onChange={onChange}
          />
        );
      const { container, rerender } = render(view(1));
      const label = `Terminal max lifetime for ${scope === 'global' ? 'all servers' : 'runner-a'}`;
      const advanced = container.querySelector('details')!;

      fireEvent.click(within(advanced).getByText('Advanced'));
      fireEvent.change(screen.getByLabelText(label), { target: { value: '2' } });
      expect(screen.getByLabelText(`${label} unit`)).toHaveValue('h');
      expect(screen.getByText('Open terminals older than 2 h will close.')).toBeVisible();
      fireEvent.click(within(advanced).getByText('Advanced'));
      rerender(view(2));
      expect(advanced).not.toHaveAttribute('open');
      fireEvent.click(within(advanced).getByText('Advanced'));

      const amount = screen.getByLabelText(label);
      expect(amount).toHaveValue(2);
      expect(screen.getByLabelText(`${label} unit`)).toHaveValue('h');
      fireEvent.submit(amount.closest('form')!);
      expect(onChange).toHaveBeenCalledWith('terminal_hard_ttl_s', 7200);
    },
  );
});
