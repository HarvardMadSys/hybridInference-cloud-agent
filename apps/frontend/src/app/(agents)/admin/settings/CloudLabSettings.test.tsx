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
import { APIError } from '@/lib/utils/errors';

import CloudLabSettings from './CloudLabSettings';

const getDefaults = vi.mocked(getCloudLabDefaults);
const saveDefaults = vi.mocked(saveCloudLabDefaults);

const defaults = {
  project: 'mad-cloudlab',
  duration: 4,
  profile_project: 'cloud-agent',
  profile_name: 'kata-node',
  ssh_username: 'researcher',
  ssh_public_key: '',
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
    expect(screen.getByLabelText('CloudLab project').closest('details')).toBeNull();
    expect(screen.getByLabelText('Initial duration (hours)').closest('details')).toBeNull();
    const advanced = screen.getByText('Advanced').closest('details');
    expect(advanced).not.toHaveAttribute('open');
    expect(advanced).toContainElement(screen.getByLabelText('Template project'));
    expect(advanced).toContainElement(screen.getByLabelText('Template name'));
    expect(advanced).toContainElement(screen.getByLabelText('CloudLab SSH user'));
    expect(advanced).toContainElement(screen.getByLabelText('CloudLab SSH public key'));
    expect(screen.getByLabelText('CloudLab SSH public key')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Save defaults' })).toBeDisabled();
  });

  it('preserves advanced edits when collapsed and submits them with important defaults', async () => {
    const updated = { ...defaults, profile_name: 'new-template', ssh_username: 'new-user' };
    saveDefaults.mockResolvedValue({ defaults: updated });
    render(<CloudLabSettings />);

    await screen.findByLabelText('CloudLab project');
    const advanced = screen.getByText('Advanced').closest('details')!;
    const summary = advanced.querySelector('summary')!;
    fireEvent.click(summary);
    expect(advanced).toHaveAttribute('open');
    const template = screen.getByLabelText('Template name');
    fireEvent.change(template, { target: { value: 'new-template' } });
    fireEvent.change(screen.getByLabelText('CloudLab SSH user'), {
      target: { value: 'new-user' },
    });
    fireEvent.click(summary);

    expect(advanced).not.toHaveAttribute('open');
    expect(screen.getByLabelText('Template name')).toBe(template);
    expect(template).toHaveValue('new-template');
    fireEvent.click(screen.getByRole('button', { name: 'Save defaults' }));

    await waitFor(() => expect(saveDefaults).toHaveBeenCalledWith(updated));
  });

  it('saves an SSH public key for the CloudLab SSH user', async () => {
    const publicKey = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKeyForTheFormOnly researcher';
    const updated = { ...defaults, ssh_public_key: publicKey };
    saveDefaults.mockResolvedValue({ defaults: updated });
    render(<CloudLabSettings />);

    await screen.findByLabelText('CloudLab project');
    fireEvent.click(screen.getByText('Advanced').closest('details')!.querySelector('summary')!);
    fireEvent.change(screen.getByLabelText('CloudLab SSH public key'), {
      target: { value: publicKey },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save defaults' }));

    await waitFor(() => expect(saveDefaults).toHaveBeenCalledWith(updated));
  });

  it('keeps an invalid SSH public key in the advanced section', async () => {
    render(<CloudLabSettings />);

    const project = await screen.findByLabelText('CloudLab project');
    fireEvent.change(project, { target: { value: 'next-project' } });
    fireEvent.change(screen.getByLabelText('CloudLab SSH public key'), {
      target: { value: 'not-a-key' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save defaults' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'SSH public key must be one OpenSSH public key.',
    );
    expect(screen.getByText('Advanced').closest('details')).toHaveAttribute('open');
    expect(screen.getByLabelText('CloudLab SSH public key')).toHaveValue('not-a-key');
    expect(saveDefaults).not.toHaveBeenCalled();
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

  it('reveals advanced defaults when the server returns a validation error', async () => {
    saveDefaults.mockRejectedValue(new APIError('VALIDATION_ERROR', 'Invalid template', 422));
    render(<CloudLabSettings />);

    const project = await screen.findByLabelText('CloudLab project');
    fireEvent.change(project, { target: { value: 'next-project' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save defaults' }));

    expect(await screen.findByRole('alert')).toHaveTextContent('Invalid template');
    const advanced = screen.getByText('Advanced').closest('details')!;
    expect(advanced).toHaveAttribute('open');
    expect(screen.getByLabelText('Template name')).toHaveValue('kata-node');

    const summary = advanced.querySelector('summary')!;
    fireEvent.click(summary);
    expect(advanced).not.toHaveAttribute('open');
    fireEvent.click(screen.getByRole('button', { name: 'Save defaults' }));
    await waitFor(() => expect(saveDefaults).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(advanced).toHaveAttribute('open'));

    fireEvent.click(summary);
    fireEvent.change(project, { target: { value: 'another-project' } });
    expect(advanced).not.toHaveAttribute('open');
  });
});
