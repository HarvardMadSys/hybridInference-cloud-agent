// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as api from '@/lib/api/compute';
import type { AutomaticComputeSetupRequest, ComputeConnection } from '@/lib/api/compute';

import ComputeSetup from './ComputeSetup';

vi.mock('@/lib/api/compute', () => ({
  getComputeAutomaticSetup: vi.fn(),
  createAutomaticComputeSetup: vi.fn(),
  saveComputeDeploymentProfile: vi.fn(),
}));

const sshConnection = (id: string, name: string): ComputeConnection => ({
  id,
  name,
  provider_id: 'existing_ssh',
  configuration: { known_hosts: 'verified public key' },
  status: 'verified',
  masked_summary: 'SSH credential configured',
  revision: 1,
  blocked_reasons: [],
});
const connections: ComputeConnection[] = [
  sshConnection('gateway-key', 'Gateway administration'),
  sshConnection('control-key', 'Control-plane administration'),
  { ...sshConnection('portal', 'CloudLab portal'), provider_id: 'cloudlab' },
];
const savedParameters: Omit<AutomaticComputeSetupRequest, 'request_id'> = {
  gateway: {
    connection_id: 'gateway-key',
    address: 'gateway.example.org',
    username: 'operator',
    port: 22,
  },
  control: {
    connection_id: 'control-key',
    address: 'control.example.org',
    username: 'operator',
    port: 22,
  },
  gateway_port: 8080,
  chat_network_pool: '172.29.0.0/16',
  dns_server: '1.1.1.1',
  protected_cidrs: ['203.0.113.0/24', '198.51.100.0/24', '192.0.2.0/24'],
  slots: 2,
};

function fillAutomaticSetup() {
  fireEvent.change(screen.getByLabelText('Gateway host address'), {
    target: { value: 'gateway.example.org' },
  });
  fireEvent.change(screen.getByLabelText('Gateway SSH username'), {
    target: { value: 'operator' },
  });
  fireEvent.change(screen.getByLabelText('Control-plane host address'), {
    target: { value: 'control.example.org' },
  });
  fireEvent.change(screen.getByLabelText('Control-plane SSH username'), {
    target: { value: 'operator' },
  });
  fireEvent.change(screen.getByLabelText('Gateway SSH connection'), {
    target: { value: 'gateway-key' },
  });
  fireEvent.change(screen.getByLabelText('Control-plane SSH connection'), {
    target: { value: 'control-key' },
  });
  fireEvent.change(screen.getByLabelText('Sandbox network pool'), {
    target: { value: '172.29.0.0/16' },
  });
  fireEvent.change(screen.getByLabelText('Sandbox DNS server'), {
    target: { value: '1.1.1.1' },
  });
  fireEvent.change(screen.getByLabelText('Protected infrastructure networks'), {
    target: { value: '203.0.113.0/24, 198.51.100.0/24\n192.0.2.0/24' },
  });
}

beforeEach(() => {
  sessionStorage.clear();
  vi.clearAllMocks();
  vi.mocked(api.getComputeAutomaticSetup).mockResolvedValue({
    available: true,
    deployment_sha: 'c'.repeat(40),
    blocked_reasons: [],
  });
  vi.mocked(api.createAutomaticComputeSetup).mockResolvedValue({
    profiles: [
      { id: 'automatic-primary', label: 'Primary' },
      { id: 'automatic-spare', label: 'Replacement spare' },
    ],
  });
});

afterEach(cleanup);

describe('Server setup', () => {
  it('creates setup without release uploads or manual secrets and uses SSH connections only', async () => {
    const saved = vi.fn();
    render(<ComputeSetup connections={connections} onSaved={saved} />);
    const configure = screen.getByRole('button', { name: 'Configure automatically' });
    await waitFor(() => expect(configure).toBeEnabled());
    expect(screen.queryByLabelText('Release bundle')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Host environment')).not.toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'CloudLab portal' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Server setup capacity')).toHaveValue(2);
    fillAutomaticSetup();
    fireEvent.click(configure);
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(api.createAutomaticComputeSetup).toHaveBeenCalledWith({
      request_id: expect.any(String),
      gateway: {
        connection_id: 'gateway-key',
        address: 'gateway.example.org',
        port: 22,
        username: 'operator',
      },
      control: {
        connection_id: 'control-key',
        address: 'control.example.org',
        port: 22,
        username: 'operator',
      },
      gateway_port: 8080,
      chat_network_pool: '172.29.0.0/16',
      dns_server: '1.1.1.1',
      protected_cidrs: ['203.0.113.0/24', '198.51.100.0/24', '192.0.2.0/24'],
      slots: 2,
    });
    expect(api.saveComputeDeploymentProfile).not.toHaveBeenCalled();
  });

  it('shows the deployment blocker and keeps manual setup available', async () => {
    vi.mocked(api.getComputeAutomaticSetup).mockResolvedValue({
      available: false,
      deployment_sha: null,
      blocked_reasons: [
        {
          code: 'release_missing',
          message: 'Redeploy the control plane with a Host release bundle.',
        },
      ],
    });
    render(<ComputeSetup connections={connections} onSaved={vi.fn()} />);
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Redeploy the control plane with a Host release bundle.',
    );
    expect(screen.getByRole('button', { name: 'Configure automatically' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Advanced manual setup' }));
    expect(screen.getByLabelText('Release bundle')).toBeInTheDocument();
    expect(screen.getByLabelText('Host environment')).toBeInTheDocument();
    expect(api.createAutomaticComputeSetup).not.toHaveBeenCalled();
  });

  it('preserves the intent after a failed response and changes it for changed inputs', async () => {
    vi.mocked(api.createAutomaticComputeSetup).mockRejectedValue(
      new Error('Connection timed out.'),
    );
    render(<ComputeSetup connections={connections} onSaved={vi.fn()} />);
    const configure = screen.getByRole('button', { name: 'Configure automatically' });
    await waitFor(() => expect(configure).toBeEnabled());
    fillAutomaticSetup();
    fireEvent.click(configure);
    expect(await screen.findByRole('alert')).toHaveTextContent('Connection timed out.');
    const firstId = vi.mocked(api.createAutomaticComputeSetup).mock.calls[0][0].request_id;
    fireEvent.click(screen.getByRole('button', { name: 'Advanced manual setup' }));
    fireEvent.click(screen.getByRole('button', { name: 'Automatic setup' }));
    expect(screen.getByLabelText('Gateway host address')).toHaveValue('gateway.example.org');
    fireEvent.click(configure);
    await waitFor(() => expect(api.createAutomaticComputeSetup).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(configure).toBeEnabled());
    expect(vi.mocked(api.createAutomaticComputeSetup).mock.calls[1][0].request_id).toBe(firstId);
    fireEvent.change(screen.getByLabelText('Sandbox DNS server'), {
      target: { value: '8.8.8.8' },
    });
    fireEvent.click(configure);
    await waitFor(() => expect(api.createAutomaticComputeSetup).toHaveBeenCalledTimes(3));
    expect(vi.mocked(api.createAutomaticComputeSetup).mock.calls[2][0].request_id).not.toBe(
      firstId,
    );
  });

  it('blocks setup while configuring and displays safe errors without changing the method', async () => {
    let rejectRequest!: (reason: Error) => void;
    vi.mocked(api.createAutomaticComputeSetup).mockImplementation(
      () =>
        new Promise((_resolve, reject) => {
          rejectRequest = reject;
        }),
    );
    const saved = vi.fn();
    render(<ComputeSetup connections={connections} onSaved={saved} />);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Configure automatically' })).toBeEnabled(),
    );
    fillAutomaticSetup();
    fireEvent.click(screen.getByRole('button', { name: 'Configure automatically' }));
    expect(screen.getByRole('button', { name: 'Configuring server setup…' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Advanced manual setup' })).toBeDisabled();
    rejectRequest(new Error('The selected SSH account needs passwordless sudo.'));
    expect(await screen.findByRole('alert')).toHaveTextContent('needs passwordless sudo');
    expect(saved).not.toHaveBeenCalled();
  });

  it('restores server-side defaults and resumes the pending request only after submission', async () => {
    vi.mocked(api.getComputeAutomaticSetup).mockResolvedValue({
      available: true,
      deployment_sha: 'c'.repeat(40),
      blocked_reasons: [],
      defaults: savedParameters,
      pending_request_id: 'setup-pending',
    });
    render(<ComputeSetup connections={connections} onSaved={vi.fn()} />);
    const retry = await screen.findByRole('button', { name: 'Retry automatic setup' });
    expect(screen.getByLabelText('Gateway host address')).toHaveValue('gateway.example.org');
    expect(screen.getByLabelText('Control-plane SSH connection')).toHaveValue('control-key');
    expect(screen.getByLabelText('Sandbox network pool')).toHaveValue('172.29.0.0/16');
    expect(api.createAutomaticComputeSetup).not.toHaveBeenCalled();
    fireEvent.click(retry);
    await waitFor(() =>
      expect(api.createAutomaticComputeSetup).toHaveBeenCalledWith({
        ...savedParameters,
        request_id: 'setup-pending',
      }),
    );
  });

  it('recovers a browser request after reload before the server has saved defaults', async () => {
    vi.mocked(api.createAutomaticComputeSetup).mockRejectedValueOnce(
      new Error('Connection timed out.'),
    );
    const initial = render(<ComputeSetup connections={connections} onSaved={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Configure automatically' })).toBeEnabled(),
    );
    fillAutomaticSetup();
    fireEvent.click(screen.getByRole('button', { name: 'Configure automatically' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Connection timed out.');
    const original = vi.mocked(api.createAutomaticComputeSetup).mock.calls[0][0];
    initial.unmount();

    const saved = vi.fn();
    const restored = render(<ComputeSetup connections={connections} onSaved={saved} />);
    const retry = await screen.findByRole('button', { name: 'Retry automatic setup' });
    expect(screen.getByLabelText('Gateway host address')).toHaveValue('gateway.example.org');
    expect(screen.getByLabelText('Sandbox DNS server')).toHaveValue('1.1.1.1');
    expect(api.createAutomaticComputeSetup).toHaveBeenCalledTimes(1);
    fireEvent.click(retry);
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(vi.mocked(api.createAutomaticComputeSetup).mock.calls[1][0]).toEqual(original);
    restored.unmount();

    render(<ComputeSetup connections={connections} onSaved={vi.fn()} />);
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Configure automatically' })).toBeEnabled(),
    );
    expect(screen.queryByRole('button', { name: 'Retry automatic setup' })).not.toBeInTheDocument();
  });

  it('reuses completed setup defaults without creating capacity until explicitly submitted', async () => {
    vi.mocked(api.getComputeAutomaticSetup).mockResolvedValue({
      available: true,
      deployment_sha: 'c'.repeat(40),
      blocked_reasons: [],
      defaults: savedParameters,
      pending_request_id: null,
    });
    render(<ComputeSetup connections={connections} onSaved={vi.fn()} />);
    const configure = screen.getByRole('button', { name: 'Configure automatically' });
    await waitFor(() => expect(configure).toBeEnabled());
    expect(screen.getByLabelText('Protected infrastructure networks')).toHaveValue(
      savedParameters.protected_cidrs.join('\n'),
    );
    expect(api.createAutomaticComputeSetup).not.toHaveBeenCalled();
    fireEvent.click(configure);
    await waitFor(() =>
      expect(api.createAutomaticComputeSetup).toHaveBeenCalledWith({
        ...savedParameters,
        request_id: expect.any(String),
      }),
    );
  });

  it('starts a fresh request for unchanged settings when the administrator explicitly restarts', async () => {
    vi.mocked(api.getComputeAutomaticSetup).mockResolvedValue({
      available: true,
      deployment_sha: 'c'.repeat(40),
      blocked_reasons: [],
      defaults: savedParameters,
      pending_request_id: 'setup-stuck',
    });
    render(<ComputeSetup connections={connections} onSaved={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Start new setup' }));
    expect(screen.getByLabelText('Gateway host address')).toHaveValue('gateway.example.org');
    expect(api.createAutomaticComputeSetup).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Configure automatically' }));
    await waitFor(() => expect(api.createAutomaticComputeSetup).toHaveBeenCalledOnce());
    const request = vi.mocked(api.createAutomaticComputeSetup).mock.calls[0][0];
    expect(request).toEqual({ ...savedParameters, request_id: expect.any(String) });
    expect(request.request_id).not.toBe('setup-stuck');
  });
});
