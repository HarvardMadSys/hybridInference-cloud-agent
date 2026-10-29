// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { setComputePolicy, type ComputeAllocation } from '@/lib/api/compute';

import ComputeResourcePanel from './ComputeResourcePanel';

vi.mock('@/lib/api/compute', async (original) => ({
  ...(await original<typeof import('@/lib/api/compute')>()),
  setComputePolicy: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

const TIMING = 'Expiry timing and provider parameter';

function allocation(policy: Partial<ComputeAllocation['policy']> = {}): ComputeAllocation {
  return {
    id: 'resource-a',
    name: 'CloudLab experiment',
    provider_id: 'cloudlab',
    connection_id: 'cloudlab-account',
    ssh_connection_id: 'ssh-account',
    state: 'ready',
    external_id: 'experiment-a',
    expires_at: '2026-10-05T08:00:00Z',
    observed_at: '2026-10-04T08:00:00Z',
    revision: 2,
    allowed_actions: ['policy'],
    blocked_reasons: [],
    nodes: [{ id: 'node-a', name: 'node 1', hosts: ['host-a'] }],
    policy: {
      enabled: false,
      renew_before_seconds: 3600,
      extend_to_horizon_seconds: 14400,
      stop_new_groups_before_seconds: 1800,
      stop_claims_before_seconds: 300,
      rotation_enabled: false,
      max_node_age_seconds: 604800,
      node_types: [],
      node_type_binding: 'phystype',
      reason: '',
      ...policy,
    },
  };
}

function renderPanel(resource: ComputeAllocation) {
  vi.mocked(setComputePolicy).mockImplementation(async (_id, policy) => ({
    allocation: { ...resource, policy, revision: 3 },
  }));
  render(<ComputeResourcePanel allocation={resource} onSaved={vi.fn()} onOperation={vi.fn()} />);
  return screen.getByRole('group', { name: TIMING });
}

describe('renewal timing fields', () => {
  it('shows every timing field directly under a descriptive heading with its saved value', () => {
    const group = renderPanel(allocation({ move_out_before_seconds: 5400 }));
    expect(group.closest('details')).toBeNull();
    expect(screen.queryByText('Advanced timing')).not.toBeInTheDocument();
    const fields = [
      ['Stop new sandboxes before expiry (minutes)', 30],
      ['Stop all new turns before expiry (minutes)', 5],
      ['Move sandboxes to other servers before expiry (minutes, 0 = never)', 90],
    ] as const;
    for (const [label, minutes] of fields) {
      const input = within(group).getByLabelText(label);
      expect(input).toBeVisible();
      expect(input).toHaveValue(minutes);
      expect(input).toBeRequired();
    }
    const binding = within(group).getByLabelText('Node type parameter of the provider profile');
    expect(binding).toBeVisible();
    expect(binding).toHaveValue('phystype');
    expect(binding).toBeRequired();
  });

  it('keeps the move-out default and hides it while node replacement handles expiry', () => {
    const group = renderPanel(allocation());
    const moveOut = within(group).getByLabelText(
      'Move sandboxes to other servers before expiry (minutes, 0 = never)',
    );
    expect(moveOut).toHaveValue(120);
    fireEvent.click(screen.getByLabelText('Request renewal automatically'));
    fireEvent.click(screen.getByLabelText(/Move to a new node when renewal/));
    expect(
      within(group).queryByLabelText(
        'Move sandboxes to other servers before expiry (minutes, 0 = never)',
      ),
    ).not.toBeInTheDocument();
    expect(
      within(group).getByLabelText('Stop new sandboxes before expiry (minutes)'),
    ).toBeVisible();
  });

  it('saves edited timing as seconds with the rest of the policy', async () => {
    const resource = allocation();
    const group = renderPanel(resource);
    fireEvent.change(within(group).getByLabelText('Stop new sandboxes before expiry (minutes)'), {
      target: { value: '45' },
    });
    fireEvent.change(within(group).getByLabelText('Stop all new turns before expiry (minutes)'), {
      target: { value: '10' },
    });
    fireEvent.change(
      within(group).getByLabelText(
        'Move sandboxes to other servers before expiry (minutes, 0 = never)',
      ),
      { target: { value: '0' } },
    );
    fireEvent.change(within(group).getByLabelText('Node type parameter of the provider profile'), {
      target: { value: 'hardware' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save renewal policy' }));
    await waitFor(() =>
      expect(setComputePolicy).toHaveBeenCalledWith(
        resource.id,
        {
          ...resource.policy,
          stop_new_groups_before_seconds: 2700,
          stop_claims_before_seconds: 600,
          move_out_before_seconds: 0,
          node_type_binding: 'hardware',
        },
        2,
        undefined,
      ),
    );
  });

  it('blocks saving while a now-visible required timing value is empty', () => {
    const group = renderPanel(allocation());
    fireEvent.change(within(group).getByLabelText('Node type parameter of the provider profile'), {
      target: { value: '' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save renewal policy' }));
    expect(setComputePolicy).not.toHaveBeenCalled();
  });
});
