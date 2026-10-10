// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAgentAdminSettings,
  setAgentAdminSettings,
  type AgentAdminSettings,
} from '@/lib/api/adminSettings';
import { APIError } from '@/lib/utils/errors';

import ApplicationSettings from './ApplicationSettings';

vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: vi.fn(),
  setAgentAdminSettings: vi.fn(),
}));

const read = vi.mocked(getAgentAdminSettings);
const write = vi.mocked(setAgentAdminSettings);

function snapshot(over: Partial<AgentAdminSettings> = {}): AgentAdminSettings {
  return {
    revision: 1,
    definitions: [
      {
        key: 'title',
        label: 'Product name',
        description: 'Name shown in the app.',
        category: 'Site',
        kind: 'string',
        default: 'Cloud Agent',
        secret: false,
      },
      {
        key: 'enabled',
        label: 'Registration',
        description: 'Allow new registrations.',
        category: 'Access',
        kind: 'boolean',
        default: true,
        secret: false,
      },
      {
        key: 'limit',
        label: 'Files per request',
        description: 'Maximum file count.',
        category: 'Limits',
        kind: 'integer',
        default: 50,
        minimum: 1,
        maximum: 100,
        secret: false,
      },
      {
        key: 'ratio',
        label: 'Retry factor',
        description: 'Retry backoff factor.',
        category: 'Limits',
        kind: 'number',
        default: 1.5,
        minimum: 1,
        maximum: 5,
        secret: false,
      },
      {
        key: 'runtime',
        label: 'Default runtime',
        description: 'Runtime offered first.',
        category: 'Site',
        kind: 'string',
        default: 'codex',
        choices: ['codex', 'claude-code'],
        secret: false,
      },
      {
        key: 'token',
        label: 'Service token',
        description: 'Credential for the upstream service.',
        category: 'Connections',
        kind: 'string',
        default: '',
        secret: true,
      },
    ],
    values: {
      title: 'Cloud Agent',
      enabled: true,
      limit: 50,
      ratio: 1.5,
      runtime: 'codex',
      token: null,
    },
    secret_configured: { token: true },
    bootstrap: [
      {
        key: 'DATABASE_URL',
        label: 'Database connection',
        description: 'Connection used to load the stored settings.',
      },
    ],
    ...over,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

beforeEach(() => {
  read.mockReset().mockResolvedValue(snapshot());
  write.mockReset();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('application settings', () => {
  it('renders typed fields, category descriptions, hidden secret status, and read-only setup', async () => {
    render(<ApplicationSettings />);
    expect(await screen.findByLabelText('Product name')).toHaveValue('Cloud Agent');
    expect(screen.getByLabelText('Registration')).toHaveValue('true');
    expect(screen.getByLabelText('Retry factor')).toHaveValue(1.5);
    expect(screen.getByLabelText('Default runtime')).toHaveValue('codex');
    expect(screen.getByLabelText('Service token')).toHaveValue('');
    expect(screen.getByText('Configured. Saved values are hidden.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Limits' })).toBeInTheDocument();
    fireEvent.click(screen.getByText('Deployment setup'));
    expect(screen.getByText('DATABASE_URL')).toBeInTheDocument();
    expect(screen.queryByLabelText('Database connection')).not.toBeInTheDocument();
  });

  it('writes false, fractional numbers, and strings without changing unrelated settings', async () => {
    write.mockResolvedValue(snapshot({ values: { ...snapshot().values, enabled: false } }));
    render(<ApplicationSettings />);
    fireEvent.change(await screen.findByLabelText('Registration'), { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Registration' }));
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith({ enabled: false }, expect.any(AbortSignal)),
    );
    expect(await screen.findByText('Registration saved.')).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Retry factor'), { target: { value: '2.75' } });
    write.mockResolvedValue(snapshot({ values: { ...snapshot().values, ratio: 2.75 } }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Retry factor' }));
    await waitFor(() =>
      expect(write).toHaveBeenLastCalledWith({ ratio: 2.75 }, expect.any(AbortSignal)),
    );
    await screen.findByText('Retry factor saved.');
    fireEvent.change(screen.getByLabelText('Product name'), { target: { value: '' } });
    write.mockResolvedValue(snapshot({ values: { ...snapshot().values, title: '' } }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Product name' }));
    await waitFor(() =>
      expect(write).toHaveBeenLastCalledWith({ title: '' }, expect.any(AbortSignal)),
    );
  });

  it('validates integer bounds before saving', async () => {
    render(<ApplicationSettings />);
    const input = await screen.findByLabelText('Files per request');
    for (const [raw, error] of [
      ['1.5', 'Enter a whole number.'],
      ['0', 'Enter a value of at least 1.'],
      ['101', 'Enter a value no greater than 100.'],
      ['', 'Enter a number.'],
    ]) {
      fireEvent.change(input, { target: { value: raw } });
      fireEvent.click(screen.getByRole('button', { name: 'Save Files per request' }));
      expect(screen.getByRole('alert')).toHaveTextContent(error);
    }
    expect(write).not.toHaveBeenCalled();
  });

  it('writes multiline secrets once and clears their draft after saving', async () => {
    write.mockResolvedValue(snapshot());
    render(<ApplicationSettings />);
    await screen.findByLabelText('Service token');
    fireEvent.click(screen.getByLabelText('Use multiple lines for Service token'));
    const input = screen.getByLabelText('Service token');
    const secret = 'private-key-line-1\nprivate-key-line-2';
    fireEvent.change(input, { target: { value: secret } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Service token' }));
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith({ token: secret }, expect.any(AbortSignal)),
    );
    await screen.findByText('Service token saved.');
    expect(screen.getByLabelText('Service token')).toHaveValue('');
    expect(screen.queryByText(secret)).not.toBeInTheDocument();
  });

  it('resets to the built-in default using null and preserves drafts in other rows', async () => {
    write.mockResolvedValue(snapshot());
    render(<ApplicationSettings />);
    fireEvent.change(await screen.findByLabelText('Product name'), {
      target: { value: 'Unsaved name' },
    });
    fireEvent.change(screen.getByLabelText('Files per request'), { target: { value: '70' } });
    fireEvent.click(screen.getByRole('button', { name: 'Reset Files per request' }));
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith({ limit: null }, expect.any(AbortSignal)),
    );
    await screen.findByText('Files per request reset to its default.');
    expect(screen.getByLabelText('Files per request')).toHaveValue(50);
    expect(screen.getByLabelText('Product name')).toHaveValue('Unsaved name');
  });

  it('keeps edits during refresh while updating pristine fields', async () => {
    render(<ApplicationSettings />);
    fireEvent.change(await screen.findByLabelText('Product name'), {
      target: { value: 'My draft' },
    });
    read.mockResolvedValue(
      snapshot({
        revision: 2,
        values: { ...snapshot().values, title: 'Another admin', limit: 80 },
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Refresh settings' }));
    await waitFor(() => expect(screen.getByLabelText('Files per request')).toHaveValue(80));
    expect(screen.getByLabelText('Product name')).toHaveValue('My draft');
  });

  it('ignores a read started before a save completes', async () => {
    const pending = deferred<AgentAdminSettings>();
    render(<ApplicationSettings />);
    await screen.findByLabelText('Product name');
    read.mockReturnValueOnce(pending.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh settings' }));
    const readSignal = read.mock.calls[1][0]!;
    write.mockResolvedValue(
      snapshot({ revision: 2, values: { ...snapshot().values, title: 'Saved name' } }),
    );
    fireEvent.change(screen.getByLabelText('Product name'), { target: { value: 'Saved name' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Product name' }));
    await screen.findByText('Product name saved.');
    expect(readSignal.aborted).toBe(true);
    await act(async () => pending.resolve(snapshot()));
    expect(screen.getByLabelText('Product name')).toHaveValue('Saved name');
  });

  it('retries an initial failure and preserves edits after a failed write', async () => {
    read.mockRejectedValueOnce(new Error('Settings temporarily unavailable'));
    render(<ApplicationSettings />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Settings temporarily unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry application settings' }));
    fireEvent.change(await screen.findByLabelText('Product name'), {
      target: { value: 'My draft' },
    });
    write.mockRejectedValue(new Error('Write failed'));
    fireEvent.click(screen.getByRole('button', { name: 'Save Product name' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Write failed');
    expect(screen.getByLabelText('Product name')).toHaveValue('My draft');
  });

  it('hides fields and stops reading after admin access is revoked', async () => {
    const denied = vi.fn();
    render(<ApplicationSettings onAccessDenied={denied} />);
    await screen.findByLabelText('Product name');
    const cause = new APIError('FORBIDDEN', 'Admin access revoked', 403);
    read.mockRejectedValueOnce(cause);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh settings' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Admin access required');
    expect(denied).toHaveBeenCalledWith(cause);
    expect(screen.queryByLabelText('Service token')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Refresh settings' })).not.toBeInTheDocument();
  });

  it('aborts an outstanding save on unmount', async () => {
    const pending = deferred<AgentAdminSettings>();
    write.mockReturnValue(pending.promise);
    const view = render(<ApplicationSettings />);
    fireEvent.change(await screen.findByLabelText('Product name'), {
      target: { value: 'My draft' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Product name' }));
    const signal = write.mock.calls[0][1]!;
    view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(snapshot()));
  });
});
