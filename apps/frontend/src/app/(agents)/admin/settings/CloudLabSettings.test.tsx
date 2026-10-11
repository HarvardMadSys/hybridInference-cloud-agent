// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AgentAccessApi } from '@/lib/api/session';

const auth = vi.hoisted(() => ({ id: 'admin-a', access: undefined as AgentAccessApi | undefined }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({ state: { user: { id: auth.id, role: 'free', agent_access: auth.access } } }),
}));
vi.mock('@/components/providers/AuthProvider', async () => {
  const actual = await vi.importActual<typeof import('@/components/providers/AuthProvider')>(
    '@/components/providers/AuthProvider',
  );
  return { hasAgentPermission: actual.hasAgentPermission };
});
vi.mock('@/lib/api/compute', () => ({
  getCloudLabDefaults: vi.fn(),
  saveCloudLabDefaults: vi.fn(),
}));

import { getCloudLabDefaults, saveCloudLabDefaults } from '@/lib/api/compute';

import CloudLabSettings from './CloudLabSettings';

const getDefaults = vi.mocked(getCloudLabDefaults);
const saveDefaults = vi.mocked(saveCloudLabDefaults);

const defaults = {
  project: 'mad-cloudlab',
  duration: 4,
  profile_project: 'cloud-agent',
  profile_name: 'kata-node',
  ssh_username: 'researcher',
};

beforeEach(() => {
  auth.id = 'admin-a';
  auth.access = { allowed: true, permissions: ['agent.use', 'agent.admin'] };
  vi.clearAllMocks();
  getDefaults.mockResolvedValue({ defaults });
});

afterEach(() => cleanup());

describe('admin CloudLab settings', () => {
  it('requires admin permission before fetching defaults', () => {
    auth.access = { allowed: true, permissions: ['agent.use'] };
    render(<CloudLabSettings />);

    expect(screen.getByText('Admin access required')).toBeInTheDocument();
    expect(getDefaults).not.toHaveBeenCalled();
  });

  it('loads the saved CloudLab defaults', async () => {
    render(<CloudLabSettings />);

    expect(await screen.findByLabelText('CloudLab project')).toHaveValue('mad-cloudlab');
    expect(screen.getByLabelText('Initial duration (hours)')).toHaveValue(4);
    expect(screen.getByLabelText('Template project')).toHaveValue('cloud-agent');
    expect(screen.getByLabelText('Template name')).toHaveValue('kata-node');
    expect(screen.getByLabelText('CloudLab SSH user')).toHaveValue('researcher');
    expect(screen.getByRole('button', { name: 'Save defaults' })).toBeDisabled();
  });

  it('saves edited defaults and uses the returned values', async () => {
    const updated = { ...defaults, project: 'next-project', duration: 12 };
    saveDefaults.mockResolvedValue({ defaults: updated });
    render(<CloudLabSettings />);

    const project = await screen.findByLabelText('CloudLab project');
    fireEvent.change(project, { target: { value: 'next-project' } });
    fireEvent.change(screen.getByLabelText('Initial duration (hours)'), {
      target: { value: '12' },
    });
    fireEvent.submit(project.closest('form')!);

    await waitFor(() => expect(saveDefaults).toHaveBeenCalledWith(updated));
    expect(await screen.findByRole('status')).toHaveTextContent(
      'CloudLab defaults saved. New server forms use them immediately.',
    );
    expect(screen.getByRole('button', { name: 'Save defaults' })).toBeDisabled();
  });

  it('keeps a failed edit available for retry', async () => {
    saveDefaults.mockRejectedValue(new Error('Settings unavailable'));
    render(<CloudLabSettings />);

    const input = await screen.findByLabelText('Template name');
    fireEvent.change(input, { target: { value: 'new-template' } });
    fireEvent.submit(input.closest('form')!);

    expect(await screen.findByRole('alert')).toHaveTextContent('Settings unavailable');
    expect(screen.getByLabelText('Template name')).toHaveValue('new-template');
    expect(screen.getByRole('button', { name: 'Save defaults' })).toBeEnabled();
  });
});
