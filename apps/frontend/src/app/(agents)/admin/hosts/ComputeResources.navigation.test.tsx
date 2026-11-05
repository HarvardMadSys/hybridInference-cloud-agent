// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import ComputeResources from './ComputeResources';

vi.mock('@/lib/api/compute', async (original) => ({
  ...(await original<typeof import('@/lib/api/compute')>()),
  getComputeProviders: vi.fn(async () => ({
    providers: [],
    deployment_profiles: [],
    secret_store_configured: true,
  })),
  getComputeConnections: vi.fn(async () => ({ connections: [] })),
  getComputeServers: vi.fn(async () => ({
    allocations: [],
    hosts: [],
    active_host: null,
    observed_at: null,
  })),
  getComputeOperations: vi.fn(async () => ({ operations: [] })),
  getComputeAutomaticSetup: vi.fn(async () => ({
    available: false,
    deployment_sha: null,
    blocked_reasons: [],
  })),
}));
afterEach(cleanup);

describe('deployment management entry into server setup', () => {
  it('opens Server setup from the deployment management link', async () => {
    window.history.replaceState(null, '', '/admin/hosts#compute-setup');
    try {
      render(<ComputeResources onHostsChanged={vi.fn()} />);
      const panel = await screen.findByRole('dialog', { name: 'Server setup' });
      expect(panel).toBeVisible();
      expect(within(panel).getByRole('heading', { name: 'Server setup' })).toBeVisible();
      fireEvent.click(within(panel).getByRole('button', { name: /close/i }));
      expect(screen.queryByRole('dialog', { name: 'Server setup' })).not.toBeInTheDocument();
      expect(window.location.hash).toBe('');
    } finally {
      window.history.replaceState(null, '', '/');
    }
  });
});
