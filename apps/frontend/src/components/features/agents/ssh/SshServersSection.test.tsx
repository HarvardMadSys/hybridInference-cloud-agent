// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  createSshServer,
  deleteSshServer,
  listSshServers,
  rotateSshServerKey,
  scanSshServer,
  verifySshServer,
  type SshServerApi,
} from '@/lib/api/sshServers';
import { SshServersSection } from './SshServersSection';

vi.mock('@/lib/api/sshServers', () => ({
  listSshServers: vi.fn(),
  createSshServer: vi.fn(),
  updateSshServer: vi.fn(),
  scanSshServer: vi.fn(),
  verifySshServer: vi.fn(),
  rotateSshServerKey: vi.fn(),
  deleteSshServer: vi.fn(),
}));

function server(overrides: Partial<SshServerApi> = {}): SshServerApi {
  return {
    id: 'srv-1',
    name: 'gpu',
    description: '',
    address: 'gpu.example.org',
    port: 22,
    username: 'ubuntu',
    status: 'pending',
    pinned_address: null,
    host_key: null,
    scan: null,
    public_key: 'ssh-ed25519 AAAAkey cloud-agent:gpu-srv1',
    install_command: "printf '%s\\n' 'ssh-ed25519 AAAAkey' >> ~/.ssh/authorized_keys",
    pending_public_key: null,
    pending_install_command: null,
    last_check_at: null,
    last_check_error: null,
    last_check_message: null,
    revision: 1,
    created_at: '2026-10-15T00:00:00Z',
    updated_at: '2026-10-15T00:00:00Z',
    ...overrides,
  };
}

const FINGERPRINT = 'SHA256:abcdefghijklmnop';

describe('SshServersSection', () => {
  beforeEach(() => {
    vi.mocked(listSshServers).mockResolvedValue({ configured: true, servers: [] });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('adds a server, shows the key to install, confirms the host key and logs in', async () => {
    const created = server();
    const scanned = server({
      revision: 3,
      scan: {
        address: '93.184.216.34',
        host_keys: [{ type: 'ssh-ed25519', fingerprint: FINGERPRINT }],
      },
    });
    const ready = server({
      revision: 4,
      status: 'ready',
      pinned_address: '93.184.216.34',
      host_key: { type: 'ssh-ed25519', fingerprint: FINGERPRINT },
    });
    vi.mocked(createSshServer).mockResolvedValue(created);
    vi.mocked(scanSshServer).mockResolvedValue(scanned);
    vi.mocked(verifySshServer).mockResolvedValue(ready);
    render(<SshServersSection />);
    expect(await screen.findByText('No servers yet.')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
    fireEvent.change(screen.getByRole('textbox', { name: /Name/ }), { target: { value: 'gpu' } });
    fireEvent.change(screen.getByRole('textbox', { name: /Address/ }), {
      target: { value: 'gpu.example.org' },
    });
    fireEvent.change(screen.getByRole('textbox', { name: /Username/ }), {
      target: { value: 'ubuntu' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() =>
      expect(createSshServer).toHaveBeenCalledWith({
        name: 'gpu',
        address: 'gpu.example.org',
        port: 22,
        username: 'ubuntu',
        description: '',
      }),
    );
    expect(await screen.findByText(created.install_command)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Read host key' }));
    expect(await screen.findByText(FINGERPRINT)).toBeInTheDocument();
    expect(
      screen.getByText(/ssh-keygen -lf \/etc\/ssh\/ssh_host_ed25519_key.pub/),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm and test login' }));

    await waitFor(() => expect(verifySshServer).toHaveBeenCalledWith(scanned, FINGERPRINT));
    expect(await screen.findByText(/gpu is ready/)).toBeInTheDocument();
    expect(screen.getAllByText('Ready').length).toBeGreaterThan(0);
  });

  it('shows why a check failed and keeps the dialog on the latest revision', async () => {
    const failed = server({ revision: 2, last_check_error: 'refused' });
    vi.mocked(listSshServers)
      .mockResolvedValueOnce({ configured: true, servers: [server()] })
      .mockResolvedValue({ configured: true, servers: [failed] });
    vi.mocked(scanSshServer)
      .mockRejectedValueOnce(new Error('The server refused the connection.'))
      .mockResolvedValue(failed);
    render(<SshServersSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'Check connection' }));
    fireEvent.click(screen.getByRole('button', { name: 'Read host key' }));

    expect(await screen.findByText('The server refused the connection.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Read host key' }));
    await waitFor(() => expect(scanSshServer).toHaveBeenLastCalledWith(failed));
  });

  it('tests a new key against the pinned host key without reading it again', async () => {
    const ready = server({
      status: 'ready',
      host_key: { type: 'ssh-ed25519', fingerprint: FINGERPRINT },
      pinned_address: '93.184.216.34',
    });
    const rotating = {
      ...ready,
      revision: 2,
      pending_public_key: 'ssh-ed25519 BBBBnew',
      pending_install_command: "printf '%s\\n' 'ssh-ed25519 BBBBnew' >> ~/.ssh/authorized_keys",
    };
    vi.mocked(listSshServers).mockResolvedValue({ configured: true, servers: [ready] });
    vi.mocked(rotateSshServerKey).mockResolvedValue(rotating);
    vi.mocked(verifySshServer).mockResolvedValue({ ...ready, revision: 3 });
    render(<SshServersSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'New key' }));

    expect(await screen.findByText(rotating.pending_install_command)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Read host key' })).not.toBeInTheDocument();
    const dialog = screen.getByRole('dialog');
    fireEvent.click(
      Array.from(dialog.querySelectorAll('button')).find(
        (button) => button.textContent === 'Test the new key',
      )!,
    );
    await waitFor(() => expect(verifySshServer).toHaveBeenCalledWith(rotating, FINGERPRINT));
  });

  it('asks before deleting and tells the owner to remove the key line', async () => {
    vi.mocked(listSshServers).mockResolvedValue({ configured: true, servers: [server()] });
    vi.mocked(deleteSshServer).mockResolvedValue(undefined);
    render(<SshServersSection />);
    fireEvent.click(await screen.findByRole('button', { name: 'Delete' }));
    expect(deleteSshServer).not.toHaveBeenCalled();
    expect(screen.getByText(/Delete gpu\? Chats lose access at once/)).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    });

    expect(deleteSshServer).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/gpu deleted/)).toBeInTheDocument();
    expect(screen.queryByText('ubuntu@gpu.example.org:22')).not.toBeInTheDocument();
  });

  it('cannot add servers where the deployment cannot store keys', async () => {
    vi.mocked(listSshServers).mockResolvedValue({ configured: false, servers: [] });
    render(<SshServersSection />);
    expect(await screen.findByText(/credential encryption key/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add server' })).toBeDisabled();
  });
});
