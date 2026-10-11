// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
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

function selectCategory(category: string) {
  const navigation = screen.getByRole('navigation', { name: 'Application setting categories' });
  fireEvent.click(within(navigation).getByRole('button', { name: category }));
}

beforeEach(() => {
  window.history.replaceState(null, '', window.location.pathname);
  read.mockReset().mockResolvedValue(snapshot());
  write.mockReset();
});

afterEach(() => {
  cleanup();
  window.history.replaceState(null, '', window.location.pathname);
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('application settings', () => {
  it('shows one category with accessible navigation and keeps other fields mounted', async () => {
    render(<ApplicationSettings />);
    const navigation = await screen.findByRole('navigation', {
      name: 'Application setting categories',
    });
    const buttons = within(navigation).getAllByRole('button');
    expect(buttons).toHaveLength(4);
    for (const [index, category] of ['Site', 'Access', 'Limits', 'Connections'].entries()) {
      const button = within(navigation).getByRole('button', { name: category });
      expect(button).toBe(buttons[index]);
      expect(within(button).getByText(String([2, 1, 2, 1][index]))).toHaveAttribute(
        'aria-hidden',
        'true',
      );
      const section = document.getElementById(button.getAttribute('aria-controls')!);
      expect(section?.tagName).toBe('SECTION');
      expect(section).toHaveAttribute('aria-labelledby');
      expect(button).toHaveAttribute('aria-pressed', index === 0 ? 'true' : 'false');
      expect(section?.hidden).toBe(index !== 0);
    }
    expect(
      within(screen.getByRole('region', { name: 'Application settings' })).getAllByRole('region'),
    ).toHaveLength(1);
    expect(screen.getByRole('region', { name: 'Site' })).toBeVisible();
    expect(screen.getByRole('textbox', { name: 'Product name' })).toHaveValue('Cloud Agent');
    expect(screen.queryByRole('combobox', { name: 'Registration' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Registration')).not.toBeVisible();

    selectCategory('Access');
    expect(
      within(screen.getByRole('region', { name: 'Application settings' })).getAllByRole('region'),
    ).toHaveLength(1);
    expect(screen.getByRole('region', { name: 'Access' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Site' })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: 'Access' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('combobox', { name: 'Registration' })).toHaveValue('true');
    expect(screen.queryByRole('textbox', { name: 'Product name' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Product name')).not.toBeVisible();
    expect(screen.getByText('Deployment setup')).toBeVisible();
  });

  it('derives categories from definitions and groups uncategorized settings under General', async () => {
    const current = snapshot();
    read.mockResolvedValue({
      ...current,
      definitions: current.definitions.slice(0, 2).map((field, index) => ({
        ...field,
        category: index === 0 ? '' : 'Integration extensions',
      })),
    });
    render(<ApplicationSettings />);
    const navigation = await screen.findByRole('navigation', {
      name: 'Application setting categories',
    });
    expect(within(navigation).getAllByRole('button')).toHaveLength(2);
    expect(within(navigation).getByRole('button', { name: 'General' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(screen.getByRole('textbox', { name: 'Product name' })).toBeVisible();
    selectCategory('Integration extensions');
    expect(screen.getByRole('combobox', { name: 'Registration' })).toBeVisible();
    expect(screen.getByRole('region', { name: 'Integration extensions' })).toBeVisible();
  });

  it('falls back to the first remaining category when a refresh removes the selected one', async () => {
    render(<ApplicationSettings />);
    fireEvent.change(await screen.findByLabelText('Product name'), {
      target: { value: 'Unsaved name' },
    });
    selectCategory('Limits');
    const current = snapshot();
    read.mockResolvedValue({
      ...current,
      definitions: current.definitions.filter((field) => field.category !== 'Limits'),
    });
    fireEvent.click(screen.getByRole('button', { name: 'Refresh settings' }));
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'Limits' })).not.toBeInTheDocument(),
    );
    expect(
      within(screen.getByRole('region', { name: 'Application settings' })).getAllByRole('region'),
    ).toHaveLength(1);
    expect(screen.getByRole('region', { name: 'Site' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Site' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('textbox', { name: 'Product name' })).toHaveValue('Unsaved name');
  });

  it('shows the empty state without category navigation when no definitions are available', async () => {
    read.mockResolvedValue(snapshot({ definitions: [] }));
    render(<ApplicationSettings />);
    expect(await screen.findByText('No application settings are available.')).toBeVisible();
    expect(
      screen.queryByRole('navigation', { name: 'Application setting categories' }),
    ).not.toBeInTheDocument();
    expect(screen.getByText('Deployment setup')).toBeVisible();
  });

  it('opens and focuses an initial deep link after the definitions load', async () => {
    window.history.replaceState(null, '', '#application-setting-limit');
    render(<ApplicationSettings />);
    const input = await screen.findByRole('spinbutton', { name: 'Files per request' });
    await waitFor(() => expect(input).toHaveFocus());
    expect(screen.getByRole('region', { name: 'Limits' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Limits' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('reveals a changed deep link without stealing focus or changing category on refresh', async () => {
    render(<ApplicationSettings />);
    const name = await screen.findByRole('textbox', { name: 'Product name' });
    const token = screen.getByLabelText('Service token');
    const scroll = vi.fn();
    token.scrollIntoView = scroll;
    window.history.replaceState(null, '', '#application-setting-token');
    fireEvent(window, new HashChangeEvent('hashchange'));
    await waitFor(() => expect(token).toHaveFocus());
    expect(token).toBeVisible();
    expect(screen.getByRole('region', { name: 'Connections' })).toBeVisible();
    expect(scroll).toHaveBeenCalledOnce();

    selectCategory('Site');
    name.focus();
    read.mockResolvedValue(
      snapshot({ revision: 2, values: { ...snapshot().values, title: 'Another admin' } }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Refresh settings' }));
    await waitFor(() => expect(name).toHaveValue('Another admin'));
    expect(name).toHaveFocus();
    expect(screen.getByRole('region', { name: 'Site' })).toBeVisible();
    expect(scroll).toHaveBeenCalledOnce();
  });

  it.each(['#application-setting-%E0%A4%A', '#application-setting-missing', '#unrelated'])(
    'ignores an invalid or unknown deep link %s',
    async (hash) => {
      window.history.replaceState(null, '', hash);
      render(<ApplicationSettings />);
      const name = await screen.findByRole('textbox', { name: 'Product name' });
      expect(screen.getByRole('region', { name: 'Site' })).toBeVisible();
      expect(name).not.toHaveFocus();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    },
  );

  it.each(['#application-setting-%E0%A4%A', '#application-setting-missing'])(
    'reveals a revisited setting after navigating through %s',
    async (hash) => {
      render(<ApplicationSettings />);
      const name = await screen.findByRole('textbox', { name: 'Product name' });
      const token = screen.getByLabelText('Service token');
      const scroll = vi.fn();
      token.scrollIntoView = scroll;
      window.history.replaceState(null, '', '#application-setting-token');
      fireEvent(window, new HashChangeEvent('hashchange'));
      await waitFor(() => expect(token).toHaveFocus());
      expect(screen.getByRole('region', { name: 'Connections' })).toBeVisible();

      selectCategory('Site');
      name.focus();
      window.history.replaceState(null, '', hash);
      fireEvent(window, new HashChangeEvent('hashchange'));
      expect(screen.getByRole('region', { name: 'Site' })).toBeVisible();
      expect(name).toHaveFocus();
      expect(scroll).toHaveBeenCalledOnce();

      window.history.replaceState(null, '', '#application-setting-token');
      fireEvent(window, new HashChangeEvent('hashchange'));
      await waitFor(() => expect(token).toHaveFocus());
      expect(screen.getByRole('region', { name: 'Connections' })).toBeVisible();
      expect(screen.getByRole('button', { name: 'Connections' })).toHaveAttribute(
        'aria-pressed',
        'true',
      );
      expect(scroll).toHaveBeenCalledTimes(2);
    },
  );

  it('prompts for missing fields and clears the prompt after they are saved', async () => {
    const required = {
      required: true,
      groups: [
        {
          key: 'site',
          label: 'Site setup',
          required: true,
          configured: false,
          needs_setup: true,
          missing: [{ key: 'title', label: 'Product name' }],
        },
      ],
      bootstrap_missing: [
        { key: 'AGENT_SETTINGS_ENCRYPTION_KEY', label: 'Settings encryption root key' },
      ],
    };
    read.mockResolvedValue(snapshot({ setup: required }));
    write.mockResolvedValue(snapshot({ setup: { ...required, required: false } }));
    render(<ApplicationSettings />);
    expect(await screen.findByText('Administrator setup required')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Product name' })).toHaveAttribute(
      'href',
      '#application-setting-title',
    );
    expect(screen.getByText(/Ask the deployment administrator/)).toHaveTextContent(
      'Settings encryption root key',
    );
    fireEvent.change(screen.getByLabelText('Product name'), { target: { value: 'Configured' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Product name' }));
    await screen.findByText('Product name saved.');
    expect(screen.queryByText('Administrator setup required')).not.toBeInTheDocument();
  });

  it('passes the in-memory recovery credential only to authenticated API calls', async () => {
    write.mockResolvedValue(snapshot());
    render(<ApplicationSettings recoveryToken="test-recovery-token" />);
    fireEvent.change(await screen.findByLabelText('Product name'), {
      target: { value: 'Configured' },
    });
    expect(read).toHaveBeenCalledWith(expect.any(AbortSignal), 'test-recovery-token');
    fireEvent.click(screen.getByRole('button', { name: 'Save Product name' }));
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith(
        { title: 'Configured' },
        expect.any(AbortSignal),
        'test-recovery-token',
      ),
    );
    await screen.findByText('Product name saved.');
    selectCategory('Connections');
    fireEvent.change(screen.getByLabelText('Service token'), {
      target: { value: 'replacement-service-token' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Service token' }));
    await waitFor(() =>
      expect(write).toHaveBeenLastCalledWith(
        { token: 'replacement-service-token' },
        expect.any(AbortSignal),
        'test-recovery-token',
      ),
    );
    await screen.findByText('Service token saved.');
    expect(document.body).not.toHaveTextContent('test-recovery-token');
  });

  it('reveals and focuses a missing setup field in another category', async () => {
    read.mockResolvedValue(
      snapshot({
        setup: {
          required: true,
          groups: [
            {
              key: 'connection',
              label: 'Service connection',
              required: true,
              configured: false,
              needs_setup: true,
              missing: [{ key: 'token', label: 'Service token' }],
            },
          ],
          bootstrap_missing: [],
        },
      }),
    );
    render(<ApplicationSettings />);
    const link = await screen.findByRole('link', { name: 'Service token' });
    const input = screen.getByLabelText('Service token');
    const scroll = vi.fn();
    input.scrollIntoView = scroll;
    expect(link).toHaveAttribute('href', '#application-setting-token');
    expect(input).not.toBeVisible();
    fireEvent.click(link);
    await waitFor(() => expect(input).toHaveFocus());
    await waitFor(() => expect(window.location.hash).toBe('#application-setting-token'));
    expect(input).toBeVisible();
    expect(screen.getByRole('button', { name: 'Connections' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(
      within(screen.getByRole('region', { name: 'Application settings' })).getAllByRole('region'),
    ).toHaveLength(1);
    expect(scroll).toHaveBeenCalledWith({ block: 'center' });
  });

  it('prevents editing or resetting a pinned identity or existing credential key', async () => {
    const current = snapshot();
    read.mockResolvedValue({
      ...current,
      definitions: current.definitions.map((field) => ({ ...field, read_only: true })),
    });
    render(<ApplicationSettings />);
    expect(await screen.findByLabelText('Product name')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Save Product name' })).toBeDisabled();
    selectCategory('Connections');
    expect(screen.getByLabelText('Service token')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Reset Service token' })).toBeDisabled();
    expect(write).not.toHaveBeenCalled();
  });

  it('renders typed fields, category descriptions, hidden secret status, and read-only setup', async () => {
    render(<ApplicationSettings />);
    expect(await screen.findByLabelText('Product name')).toHaveValue('Cloud Agent');
    expect(screen.getByLabelText('Default runtime')).toHaveValue('codex');
    selectCategory('Access');
    expect(screen.getByLabelText('Registration')).toHaveValue('true');
    selectCategory('Limits');
    expect(screen.getByLabelText('Retry factor')).toHaveValue(1.5);
    expect(screen.getByRole('heading', { name: 'Limits' })).toBeInTheDocument();
    selectCategory('Connections');
    expect(screen.getByLabelText('Service token')).toHaveValue('');
    expect(screen.getByText('Configured. Saved values are hidden.')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Deployment setup'));
    expect(screen.getByText('DATABASE_URL')).toBeInTheDocument();
    expect(screen.queryByLabelText('Database connection')).not.toBeInTheDocument();
  });

  it('writes false, fractional numbers, and strings without changing unrelated settings', async () => {
    write.mockResolvedValue(snapshot({ values: { ...snapshot().values, enabled: false } }));
    render(<ApplicationSettings />);
    await screen.findByLabelText('Product name');
    selectCategory('Access');
    fireEvent.change(screen.getByLabelText('Registration'), { target: { value: 'false' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save Registration' }));
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith({ enabled: false }, expect.any(AbortSignal)),
    );
    expect(await screen.findByText('Registration saved.')).toBeInTheDocument();
    selectCategory('Limits');
    fireEvent.change(screen.getByLabelText('Retry factor'), { target: { value: '2.75' } });
    write.mockResolvedValue(snapshot({ values: { ...snapshot().values, ratio: 2.75 } }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Retry factor' }));
    await waitFor(() =>
      expect(write).toHaveBeenLastCalledWith({ ratio: 2.75 }, expect.any(AbortSignal)),
    );
    await screen.findByText('Retry factor saved.');
    selectCategory('Site');
    fireEvent.change(screen.getByLabelText('Product name'), { target: { value: '' } });
    write.mockResolvedValue(snapshot({ values: { ...snapshot().values, title: '' } }));
    fireEvent.click(screen.getByRole('button', { name: 'Save Product name' }));
    await waitFor(() =>
      expect(write).toHaveBeenLastCalledWith({ title: '' }, expect.any(AbortSignal)),
    );
  });

  it('validates integer bounds before saving', async () => {
    render(<ApplicationSettings />);
    await screen.findByLabelText('Product name');
    selectCategory('Limits');
    const input = screen.getByLabelText('Files per request');
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
    await screen.findByLabelText('Product name');
    selectCategory('Connections');
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

  it('preserves drafts, multiline controls, and field errors while saving another category', async () => {
    write.mockResolvedValue(snapshot({ values: { ...snapshot().values, enabled: false } }));
    render(<ApplicationSettings />);
    fireEvent.change(await screen.findByLabelText('Product name'), {
      target: { value: 'Unsaved name' },
    });
    selectCategory('Connections');
    fireEvent.click(screen.getByRole('checkbox', { name: 'Use multiple lines for Service token' }));
    const secretInput = screen.getByLabelText('Service token');
    const secret = 'private-key-line-1\nprivate-key-line-2';
    fireEvent.change(secretInput, { target: { value: secret } });
    expect(secretInput.tagName).toBe('TEXTAREA');

    selectCategory('Limits');
    fireEvent.change(screen.getByRole('spinbutton', { name: 'Files per request' }), {
      target: { value: '101' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Files per request' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a value no greater than 100.');

    selectCategory('Access');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('combobox', { name: 'Registration' }), {
      target: { value: 'false' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save Registration' }));
    await screen.findByText('Registration saved.');
    expect(write).toHaveBeenCalledExactlyOnceWith({ enabled: false }, expect.any(AbortSignal));

    selectCategory('Limits');
    expect(screen.getByRole('spinbutton', { name: 'Files per request' })).toHaveValue(101);
    expect(screen.getByRole('spinbutton', { name: 'Files per request' })).toHaveAttribute(
      'aria-invalid',
      'true',
    );
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a value no greater than 100.');
    selectCategory('Site');
    expect(screen.getByRole('textbox', { name: 'Product name' })).toHaveValue('Unsaved name');
    selectCategory('Connections');
    expect(screen.getByLabelText('Service token')).toBe(secretInput);
    expect(secretInput).toHaveValue(secret);
    expect(
      screen.getByRole('checkbox', { name: 'Use multiple lines for Service token' }),
    ).toBeChecked();
    expect(screen.getByRole('button', { name: 'Save Service token' })).toBeEnabled();
  });

  it('resets to the built-in default using null and preserves drafts in other rows', async () => {
    write.mockResolvedValue(snapshot());
    render(<ApplicationSettings />);
    fireEvent.change(await screen.findByLabelText('Product name'), {
      target: { value: 'Unsaved name' },
    });
    selectCategory('Limits');
    fireEvent.change(screen.getByLabelText('Files per request'), { target: { value: '70' } });
    fireEvent.click(screen.getByRole('button', { name: 'Reset Files per request' }));
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith({ limit: null }, expect.any(AbortSignal)),
    );
    await screen.findByText('Files per request reset to its default.');
    expect(screen.getByLabelText('Files per request')).toHaveValue(50);
    selectCategory('Site');
    expect(screen.getByLabelText('Product name')).toHaveValue('Unsaved name');
  });

  it('keeps edits during refresh while updating pristine fields', async () => {
    render(<ApplicationSettings />);
    fireEvent.change(await screen.findByLabelText('Product name'), {
      target: { value: 'My draft' },
    });
    selectCategory('Limits');
    read.mockResolvedValue(
      snapshot({
        revision: 2,
        values: { ...snapshot().values, title: 'Another admin', limit: 80 },
      }),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Refresh settings' }));
    await waitFor(() => expect(screen.getByLabelText('Files per request')).toHaveValue(80));
    expect(screen.getByRole('button', { name: 'Limits' })).toHaveAttribute('aria-pressed', 'true');
    selectCategory('Site');
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
    vi.useFakeTimers();
    const denied = vi.fn();
    render(<ApplicationSettings onAccessDenied={denied} />);
    await act(async () => {});
    expect(screen.getByLabelText('Product name')).toBeInTheDocument();
    selectCategory('Connections');
    const cause = new APIError('FORBIDDEN', 'Admin access revoked', 403);
    read.mockRejectedValueOnce(cause);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Refresh settings' }));
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Admin access required');
    expect(denied).toHaveBeenCalledWith(cause);
    expect(screen.queryByLabelText('Service token')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Product name')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('navigation', { name: 'Application setting categories' }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Refresh settings' })).not.toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(90_000);
    });
    expect(read).toHaveBeenCalledTimes(2);
    expect(write).not.toHaveBeenCalled();
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
