// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  getAgentAdminSettings,
  setAgentAdminSettings,
  type AdminSettingDefinition,
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

function archiveSnapshot(over: Partial<AgentAdminSettings> = {}): AgentAdminSettings {
  const current = snapshot();
  return {
    ...current,
    definitions: [
      ...current.definitions,
      {
        key: 'sandbox_archive.bucket',
        label: 'Sandbox archive S3 bucket',
        description: 'S3 bucket for sandbox deletion.',
        category: 'Sandbox archives',
        kind: 'string',
        default: '',
        secret: false,
      },
      {
        key: 'sandbox_archive.prefix',
        label: 'Sandbox archive S3 prefix',
        description: 'Object key prefix for new deletion requests.',
        category: 'Sandbox archives',
        kind: 'string',
        default: 'sandbox-archives',
        secret: false,
      },
    ],
    values: {
      ...current.values,
      'sandbox_archive.bucket': '',
      'sandbox_archive.prefix': 'sandbox-archives',
    },
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

function editField(label: string): HTMLElement {
  let field = screen.getByLabelText(label);
  if (!field.matches('input,select,textarea')) {
    const group = field.closest('form')!.parentElement!;
    const edit = within(group).queryByRole('button', { name: /^Edit .* settings$/ });
    if (edit) fireEvent.click(edit);
    const replace = within(group).queryByRole('button', {
      name: new RegExp(`^(Replace|Set) ${label}$`),
    });
    if (replace) fireEvent.click(replace);
    field = screen.getByLabelText(label);
  }
  return field;
}
async function loadedField(label: string) {
  await screen.findByLabelText(label);
  return editField(label);
}
function reviewButton(label: string) {
  return within(editField(label).closest('form')!).getByRole('button', {
    name: /^Review .* changes$/,
  });
}
function saveField(label: string) {
  fireEvent.click(reviewButton(label));
  const dialog = screen.queryByRole('dialog', { name: 'Review changes' });
  if (dialog) fireEvent.click(within(dialog).getByRole('button', { name: 'Apply changes' }));
}
function resetField(label: string) {
  const group = editField(label).closest('form')!;
  fireEvent.click(within(group).getByLabelText(`More actions for ${label}`));
  fireEvent.click(within(group).getByRole('button', { name: `Restore default for ${label}` }));
  expect(write).not.toHaveBeenCalled();
  fireEvent.click(
    within(screen.getByRole('dialog')).getByRole('button', { name: 'Restore default' }),
  );
}

function organizedSnapshot(): AgentAdminSettings {
  const fields: [
    string,
    string,
    string,
    'string' | 'boolean' | 'integer',
    string | number | boolean,
  ][] = [
    ['site.app_name', 'Appearance', 'Application name', 'string', 'Cloud Agent'],
    [
      'gateway_authorize_url',
      'Sign-in',
      'Sign-in authorization URL',
      'string',
      'https://gateway.example/authorize',
    ],
    ['trust_proxy_headers', 'Security', 'Trust forwarded client addresses', 'boolean', false],
    ['github_app_id', 'Source control', 'GitHub App ID', 'string', '123'],
    ['publish_base_branch', 'Publication', 'Default pull request base branch', 'string', 'dev'],
    ['publish_git_timeout_s', 'Publication', 'Publication Git timeout', 'integer', 120],
  ];
  const definitions: AdminSettingDefinition[] = fields.map(
    ([key, category, label, kind, value]) => ({
      key,
      category,
      label,
      kind,
      default: value,
      description: '',
      secret: false,
    }),
  );
  return snapshot({
    definitions,
    values: Object.fromEntries(fields.map(([key, , , , value]) => [key, value])),
    secret_configured: {},
    bootstrap: [],
  });
}

function toggleAdvanced(field: HTMLElement) {
  fireEvent.click(field.closest('details')!.querySelector('summary')!);
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
  it('starts read-only and discards an entire group without writing', async () => {
    render(<ApplicationSettings />);
    expect(await screen.findByLabelText('Product name')).toHaveTextContent('Cloud Agent');
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Restore default/ })).not.toBeInTheDocument();
    fireEvent.change(editField('Product name'), { target: { value: 'Accidental edit' } });
    fireEvent.change(editField('Default runtime'), { target: { value: 'claude-code' } });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel editing Site' }));
    expect(screen.getByLabelText('Product name')).toHaveTextContent('Cloud Agent');
    expect(screen.getByLabelText('Default runtime')).toHaveTextContent('codex');
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Edit Site settings' })).toHaveFocus(),
    );
    expect(editField('Product name')).toHaveValue('Cloud Agent');
    expect(reviewButton('Product name')).toBeDisabled();
    expect(write).not.toHaveBeenCalled();
  });

  it('reviews both old and new values and writes a group only after confirmation', async () => {
    const current = snapshot();
    write.mockResolvedValue(
      snapshot({ values: { ...current.values, title: 'New app', runtime: 'claude-code' } }),
    );
    render(<ApplicationSettings />);
    fireEvent.change(await loadedField('Product name'), { target: { value: 'New app' } });
    fireEvent.change(editField('Default runtime'), { target: { value: 'claude-code' } });
    fireEvent.submit(editField('Product name').closest('form')!);
    const dialog = screen.getByRole('dialog', { name: 'Review changes' });
    expect(within(dialog).getByText('Cloud Agent')).toBeVisible();
    expect(within(dialog).getByText('New app')).toBeVisible();
    expect(within(dialog).getByText('codex')).toBeVisible();
    expect(within(dialog).getByText('claude-code')).toBeVisible();
    expect(within(dialog).getByRole('button', { name: 'Back to editing' })).toHaveFocus();
    expect(write).not.toHaveBeenCalled();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(editField('Product name')).toHaveValue('New app');
    saveField('Product name');
    await screen.findByText('Site settings saved.');
    expect(write).toHaveBeenCalledExactlyOnceWith(
      { title: 'New app', runtime: 'claude-code' },
      expect.any(AbortSignal),
    );
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('shows the default and lets an admin cancel a reset without losing a draft', async () => {
    render(<ApplicationSettings />);
    fireEvent.change(await loadedField('Product name'), { target: { value: 'Keep this draft' } });
    fireEvent.click(screen.getByLabelText('More actions for Product name'));
    fireEvent.click(screen.getByRole('button', { name: 'Restore default for Product name' }));
    const dialog = screen.getByRole('dialog', { name: 'Restore default value?' });
    expect(within(dialog).getByText('Default value')).toBeVisible();
    expect(within(dialog).getAllByText('Cloud Agent')).toHaveLength(2);
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(screen.getByLabelText('Product name')).toHaveValue('Keep this draft');
    expect(screen.getByLabelText('More actions for Product name')).toHaveFocus();
    expect(write).not.toHaveBeenCalled();
  });

  it('requires explicit secret replacement and keeps the new secret out of its review', async () => {
    write.mockResolvedValue(snapshot());
    render(<ApplicationSettings />);
    await screen.findByLabelText('Product name');
    selectCategory('Connections');
    fireEvent.click(screen.getByRole('button', { name: 'Edit Connections settings' }));
    expect(screen.getByLabelText('Service token')).toHaveTextContent('Configured');
    expect(screen.queryByPlaceholderText('Enter new secret')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Replace Service token' }));
    expect(screen.getByRole('button', { name: 'Review Connections changes' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('Service token'), {
      target: { value: 'new-private-token' },
    });
    fireEvent.click(reviewButton('Service token'));
    const dialog = screen.getByRole('dialog');
    expect(dialog).not.toHaveTextContent('new-private-token');
    expect(within(dialog).getByText('New secret · hidden')).toBeVisible();
    expect(write).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Apply changes' }));
    await screen.findByText('Connections settings saved.');
    expect(write).toHaveBeenCalledExactlyOnceWith(
      { token: 'new-private-token' },
      expect.any(AbortSignal),
    );
    expect(screen.queryByPlaceholderText('Enter new secret')).not.toBeInTheDocument();
  });

  it('confirms clearing a configured secret and sends null only after approval', async () => {
    write.mockResolvedValue(snapshot({ secret_configured: { token: false } }));
    render(<ApplicationSettings />);
    await screen.findByLabelText('Product name');
    selectCategory('Connections');
    fireEvent.click(screen.getByRole('button', { name: 'Edit Connections settings' }));
    fireEvent.click(screen.getByLabelText('More actions for Service token'));
    fireEvent.click(screen.getByRole('button', { name: 'Clear Service token' }));
    const dialog = screen.getByRole('dialog', { name: 'Clear saved secret?' });
    expect(dialog).toHaveTextContent('secret will be cleared');
    expect(dialog).toHaveTextContent('may stop working');
    expect(write).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Clear secret' }));
    await screen.findByText('Service token cleared.');
    expect(write).toHaveBeenCalledExactlyOnceWith({ token: null }, expect.any(AbortSignal));
    expect(screen.getByLabelText('Service token')).toHaveTextContent('Not configured');
  });

  it('warns before leaving with drafts in an inactive category', async () => {
    render(<ApplicationSettings />);
    fireEvent.change(await loadedField('Product name'), { target: { value: 'Draft app' } });
    selectCategory('Access');
    const event = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(write).not.toHaveBeenCalled();
  });

  it('merges related categories while keeping settings from each original category', async () => {
    const categoryFields = [
      ['gateway_authorize_url', 'Sign-in', 'Sign-in authorization URL'],
      ['session_cookie_secure', 'Security', 'Require HTTPS session cookies'],
      ['github_app_id', 'Source control', 'GitHub App ID'],
      ['publish_base_branch', 'Publication', 'Default pull request base branch'],
      ['attachments.max_files', 'Attachments', 'Maximum upload file count'],
      ['skills.max_selected', 'Skills', 'Maximum selected skills'],
      ['task_titles.max_chars', 'Task titles', 'Maximum task title length'],
      ['alert_webhook_url', 'Operations', 'Alert webhook URL'],
      ['goals.stalled_after_seconds', 'Goals', 'Goal stall timeout'],
      ['pgadmin_url', 'Database tools', 'Private pgAdmin URL'],
      ['compute.cloudlab_api_url', 'Compute', 'CloudLab API URL'],
      ['default_execution_profile', 'Execution', 'New session isolation policy'],
    ];
    read.mockResolvedValue(
      snapshot({
        definitions: categoryFields.map(([key, category, label]) => ({
          key,
          category,
          label,
          description: '',
          kind: 'string',
          default: '',
          secret: false,
        })),
        values: Object.fromEntries(categoryFields.map(([key]) => [key, ''])),
        secret_configured: {},
        bootstrap: [],
      }),
    );
    render(<ApplicationSettings />);
    const navigation = await screen.findByRole('navigation', {
      name: 'Application setting categories',
    });
    expect(
      within(navigation)
        .getAllByRole('button')
        .map((button) => button.textContent),
    ).toEqual(['Identity & access2', 'Source control2', 'Content3', 'Operations3', 'Compute2']);
    expect(screen.getByRole('heading', { name: 'Sign-in', level: 4 })).toBeVisible();
    expect(screen.getByRole('heading', { name: 'Session security', level: 4 })).toBeVisible();
    for (const [category, labels] of [
      ['Identity & access', ['Sign-in authorization URL', 'Require HTTPS session cookies']],
      ['Source control', ['GitHub App ID', 'Default pull request base branch']],
      [
        'Content',
        ['Maximum upload file count', 'Maximum selected skills', 'Maximum task title length'],
      ],
      ['Operations', ['Alert webhook URL', 'Goal stall timeout']],
      ['Compute', ['CloudLab API URL', 'New session isolation policy']],
    ] as const) {
      selectCategory(category);
      for (const label of labels) expect(screen.getByLabelText(label)).toBeVisible();
    }
    expect(screen.getByLabelText('Private pgAdmin URL')).toBeInTheDocument();
  });

  it('keeps Important visible and preserves Advanced drafts when closed or switched to another category', async () => {
    read.mockResolvedValue(organizedSnapshot());
    render(<ApplicationSettings />);
    await loadedField('Application name');
    selectCategory('Source control');
    const appId = editField('GitHub App ID');
    const timeout = editField('Publication Git timeout');
    expect(appId).toBeVisible();
    expect(editField('Default pull request base branch')).toBeVisible();
    expect(timeout).not.toBeVisible();
    expect(timeout.closest('details')).not.toHaveAttribute('open');
    fireEvent.change(appId, { target: { value: '456' } });
    toggleAdvanced(timeout);
    expect(timeout).toBeVisible();
    fireEvent.change(timeout, { target: { value: '180' } });
    toggleAdvanced(timeout);
    expect(timeout).not.toBeVisible();
    expect(appId).toHaveValue('456');
    selectCategory('Appearance');
    selectCategory('Source control');
    toggleAdvanced(timeout);
    expect(editField('Publication Git timeout')).toBe(timeout);
    expect(timeout).toHaveValue(180);
    expect(appId).toHaveValue('456');
    expect(write).not.toHaveBeenCalled();
  });

  it('opens Advanced and focuses an initial deep link from a merged original category', async () => {
    read.mockResolvedValue(organizedSnapshot());
    window.history.replaceState(null, '', '#application-setting-publish_git_timeout_s');
    render(<ApplicationSettings />);
    const input = await screen.findByLabelText('Publication Git timeout');
    await waitFor(() => expect(input).toHaveFocus());
    expect(input.closest('details')).toHaveAttribute('open');
    expect(screen.getByRole('region', { name: 'Source control' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Source control' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('opens Advanced in the merged category when a missing setup field is selected', async () => {
    read.mockResolvedValue({
      ...organizedSnapshot(),
      setup: {
        required: true,
        groups: [
          {
            key: 'security',
            label: 'Session security',
            required: true,
            configured: false,
            needs_setup: true,
            missing: [{ key: 'trust_proxy_headers', label: 'Trust forwarded client addresses' }],
          },
        ],
        bootstrap_missing: [],
      },
    });
    render(<ApplicationSettings />);
    const link = await screen.findByRole('link', { name: 'Trust forwarded client addresses' });
    const input = screen.getByLabelText('Trust forwarded client addresses');
    expect(input).not.toBeVisible();
    fireEvent.click(link);
    await waitFor(() => expect(input).toHaveFocus());
    expect(input).toBeVisible();
    expect(input.closest('details')).toHaveAttribute('open');
    expect(screen.getByRole('region', { name: 'Identity & access' })).toBeVisible();
    expect(screen.getByLabelText('Sign-in authorization URL')).toBeVisible();
  });

  it('saves an archive URI as an atomic bucket and prefix patch and resets both drafts', async () => {
    const current = archiveSnapshot();
    read.mockResolvedValue(current);
    write.mockResolvedValue(
      archiveSnapshot({
        revision: 2,
        values: {
          ...current.values,
          'sandbox_archive.bucket': 'freeinference',
          'sandbox_archive.prefix': 'cloud-agent/sandbox',
        },
      }),
    );
    render(<ApplicationSettings />);
    fireEvent.change(await loadedField('Product name'), {
      target: { value: 'Unsaved name' },
    });
    selectCategory('Sandbox archives');
    editField('Sandbox archive S3 bucket');
    expect(screen.getByText(/Saving an S3 URI replaces the prefix/)).toBeInTheDocument();
    fireEvent.change(editField('Sandbox archive S3 prefix'), {
      target: { value: 'unsaved-prefix' },
    });
    fireEvent.change(editField('Sandbox archive S3 bucket'), {
      target: { value: 's3://freeinference/cloud-agent/sandbox' },
    });
    saveField('Sandbox archive S3 bucket');
    await screen.findByText('Deletion archive storage settings saved.');
    expect(write).toHaveBeenCalledExactlyOnceWith(
      {
        'sandbox_archive.bucket': 'freeinference',
        'sandbox_archive.prefix': 'cloud-agent/sandbox',
      },
      expect.any(AbortSignal),
    );
    expect(editField('Sandbox archive S3 bucket')).toHaveValue('freeinference');
    expect(editField('Sandbox archive S3 prefix')).toHaveValue('cloud-agent/sandbox');
    expect(reviewButton('Sandbox archive S3 bucket')).toBeDisabled();
    expect(reviewButton('Sandbox archive S3 prefix')).toBeDisabled();
    selectCategory('Site');
    expect(editField('Product name')).toHaveValue('Unsaved name');
  });

  it('saves a bare archive bucket and an explicitly edited prefix together', async () => {
    const current = archiveSnapshot();
    read.mockResolvedValue(current);
    write.mockResolvedValue(
      archiveSnapshot({
        values: {
          ...current.values,
          'sandbox_archive.bucket': 'freeinference',
          'sandbox_archive.prefix': 'unsaved-prefix',
        },
      }),
    );
    render(<ApplicationSettings />);
    await loadedField('Product name');
    selectCategory('Sandbox archives');
    fireEvent.change(editField('Sandbox archive S3 prefix'), {
      target: { value: 'unsaved-prefix' },
    });
    fireEvent.change(editField('Sandbox archive S3 bucket'), {
      target: { value: 'freeinference' },
    });
    saveField('Sandbox archive S3 bucket');
    await screen.findByText('Deletion archive storage settings saved.');
    expect(write).toHaveBeenCalledExactlyOnceWith(
      { 'sandbox_archive.bucket': 'freeinference', 'sandbox_archive.prefix': 'unsaved-prefix' },
      expect.any(AbortSignal),
    );
    expect(editField('Sandbox archive S3 prefix')).toHaveValue('unsaved-prefix');
    expect(reviewButton('Sandbox archive S3 prefix')).toBeDisabled();
  });

  it.each([
    ['s3://freeinference/cloud-agent/sandbox/', 'cloud-agent/sandbox'],
    ['s3://freeinference', ''],
    ['s3://freeinference/', ''],
    [`s3://freeinference/${'a'.repeat(512)}`, 'a'.repeat(512)],
  ])('saves the canonical prefix for %s', async (uri, prefix) => {
    const current = archiveSnapshot();
    read.mockResolvedValue(current);
    write.mockResolvedValue(
      archiveSnapshot({
        values: {
          ...current.values,
          'sandbox_archive.bucket': 'freeinference',
          'sandbox_archive.prefix': prefix,
        },
      }),
    );
    render(<ApplicationSettings />);
    await loadedField('Product name');
    selectCategory('Sandbox archives');
    fireEvent.change(editField('Sandbox archive S3 bucket'), {
      target: { value: uri },
    });
    saveField('Sandbox archive S3 bucket');
    await screen.findByText('Deletion archive storage settings saved.');
    expect(write).toHaveBeenCalledExactlyOnceWith(
      { 'sandbox_archive.bucket': 'freeinference', 'sandbox_archive.prefix': prefix },
      expect.any(AbortSignal),
    );
    expect(editField('Sandbox archive S3 bucket')).toHaveValue('freeinference');
    expect(editField('Sandbox archive S3 prefix')).toHaveValue(prefix);
  });

  it('rejects malformed archive buckets and URIs before making an API call', async () => {
    read.mockResolvedValue(archiveSnapshot());
    render(<ApplicationSettings />);
    await loadedField('Product name');
    selectCategory('Sandbox archives');
    const input = editField('Sandbox archive S3 bucket');
    for (const value of [
      's3:/freeinference/cloud-agent/sandbox',
      'S3://freeinference/cloud-agent/sandbox',
      'https://freeinference/cloud-agent/sandbox',
      's3:///cloud-agent/sandbox',
      's3://user@freeinference/cloud-agent/sandbox',
      's3://freeinference:443/cloud-agent/sandbox',
      's3://FreeInference/cloud-agent/sandbox',
      's3://freeinference/cloud agent/sandbox',
      's3://freeinference/cloud-agent/sandbox?version=1',
      's3://freeinference/cloud-agent/sandbox#fragment',
      's3://freeinference//',
      's3://freeinference/cloud-agent//sandbox',
      's3://freeinference/cloud-agent/sandbox//',
      's3://freeinference/cloud-agent/./sandbox',
      's3://freeinference/cloud-agent/../sandbox',
      's3://freeinference/cloud-agent/%2e%2e/sandbox',
      's3://freeinference/cloud-agent%2fsandbox',
      `s3://freeinference/${'a'.repeat(513)}`,
      ' s3://freeinference/cloud-agent/sandbox',
      'invalid..bucket',
      'invalid.-bucket',
      'invalid-.bucket',
      '127.0.0.1',
      'xn--bucket',
      'sthree-bucket',
      'amzn-s3-demo-bucket',
      'bucket-s3alias',
      'bucket--ol-s3',
      'bucket.mrap',
      'bucket--x-s3',
      'bucket--table-s3',
      'ab',
      'a'.repeat(64),
    ]) {
      fireEvent.change(input, { target: { value } });
      saveField('Sandbox archive S3 bucket');
      expect(screen.getByRole('alert')).toHaveTextContent(
        /Enter (a valid S3 bucket name|an S3 URI)/,
      );
      expect(input).toHaveAttribute('aria-invalid', 'true');
      expect(write).not.toHaveBeenCalled();
    }
  });

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
    expect(screen.getByLabelText('Product name')).toHaveTextContent('Cloud Agent');
    expect(screen.queryByRole('combobox', { name: 'Registration' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Registration')).not.toBeVisible();

    selectCategory('Access');
    expect(
      within(screen.getByRole('region', { name: 'Application settings' })).getAllByRole('region'),
    ).toHaveLength(1);
    expect(screen.getByRole('region', { name: 'Access' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Site' })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: 'Access' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByLabelText('Registration')).toHaveTextContent('Enabled');
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
    expect(screen.getByLabelText('Product name')).toBeVisible();
    selectCategory('Integration extensions');
    expect(screen.getByLabelText('Registration')).toBeVisible();
    expect(screen.getByRole('region', { name: 'Integration extensions' })).toBeVisible();
  });

  it('falls back to the first remaining category when a refresh removes the selected one', async () => {
    render(<ApplicationSettings />);
    fireEvent.change(await loadedField('Product name'), {
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
    expect(editField('Product name')).toHaveValue('Unsaved name');
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
    const input = await screen.findByLabelText('Files per request');
    await waitFor(() => expect(input).toHaveFocus());
    expect(screen.getByRole('region', { name: 'Limits' })).toBeVisible();
    expect(screen.getByRole('button', { name: 'Limits' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('reveals a changed deep link without stealing focus or changing category on refresh', async () => {
    render(<ApplicationSettings />);
    const name = await screen.findByLabelText('Product name');
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
    await waitFor(() => expect(name).toHaveTextContent('Another admin'));
    expect(name).toHaveFocus();
    expect(screen.getByRole('region', { name: 'Site' })).toBeVisible();
    expect(scroll).toHaveBeenCalledOnce();
  });

  it.each(['#application-setting-%E0%A4%A', '#application-setting-missing', '#unrelated'])(
    'ignores an invalid or unknown deep link %s',
    async (hash) => {
      window.history.replaceState(null, '', hash);
      render(<ApplicationSettings />);
      const name = await screen.findByLabelText('Product name');
      expect(screen.getByRole('region', { name: 'Site' })).toBeVisible();
      expect(name).not.toHaveFocus();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    },
  );

  it.each(['#application-setting-%E0%A4%A', '#application-setting-missing'])(
    'reveals a revisited setting after navigating through %s',
    async (hash) => {
      render(<ApplicationSettings />);
      const name = await screen.findByLabelText('Product name');
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
    fireEvent.change(editField('Product name'), { target: { value: 'Configured' } });
    saveField('Product name');
    await screen.findByText('Site settings saved.');
    expect(screen.queryByText('Administrator setup required')).not.toBeInTheDocument();
  });

  it('passes the in-memory recovery credential only to authenticated API calls', async () => {
    write.mockResolvedValue(snapshot());
    render(<ApplicationSettings recoveryToken="test-recovery-token" />);
    fireEvent.change(await loadedField('Product name'), {
      target: { value: 'Configured' },
    });
    expect(read).toHaveBeenCalledWith(expect.any(AbortSignal), 'test-recovery-token');
    saveField('Product name');
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith(
        { title: 'Configured' },
        expect.any(AbortSignal),
        'test-recovery-token',
      ),
    );
    await screen.findByText('Site settings saved.');
    selectCategory('Connections');
    editField('Service token');
    fireEvent.change(editField('Service token'), {
      target: { value: 'replacement-service-token' },
    });
    saveField('Service token');
    await waitFor(() =>
      expect(write).toHaveBeenLastCalledWith(
        { token: 'replacement-service-token' },
        expect.any(AbortSignal),
        'test-recovery-token',
      ),
    );
    await screen.findByText('Connections settings saved.');
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
    expect(await screen.findByLabelText('Product name')).toHaveTextContent('Read only');
    expect(screen.getByRole('button', { name: 'Edit Site settings' })).toBeDisabled();
    selectCategory('Connections');
    expect(screen.getByLabelText('Service token')).toHaveTextContent('Read only');
    expect(screen.getByRole('button', { name: 'Edit Connections settings' })).toBeDisabled();
    expect(write).not.toHaveBeenCalled();
  });

  it('renders typed fields, category descriptions, hidden secret status, and read-only setup', async () => {
    render(<ApplicationSettings />);
    expect(await loadedField('Product name')).toHaveValue('Cloud Agent');
    expect(editField('Default runtime')).toHaveValue('codex');
    selectCategory('Access');
    expect(editField('Registration')).toHaveValue('true');
    selectCategory('Limits');
    expect(editField('Retry factor')).toHaveValue(1.5);
    expect(screen.getByRole('heading', { name: 'Limits', level: 3 })).toBeInTheDocument();
    selectCategory('Connections');
    editField('Service token');
    expect(editField('Service token')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Review Connections changes' })).toBeDisabled();
    fireEvent.click(screen.getByText('Deployment setup'));
    expect(screen.getByText('DATABASE_URL')).toBeInTheDocument();
    expect(screen.queryByLabelText('Database connection')).not.toBeInTheDocument();
  });

  it('writes false, fractional numbers, and strings without changing unrelated settings', async () => {
    write.mockResolvedValue(snapshot({ values: { ...snapshot().values, enabled: false } }));
    render(<ApplicationSettings />);
    await loadedField('Product name');
    selectCategory('Access');
    fireEvent.change(editField('Registration'), { target: { value: 'false' } });
    saveField('Registration');
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith({ enabled: false }, expect.any(AbortSignal)),
    );
    expect(await screen.findByText('Access settings saved.')).toBeInTheDocument();
    selectCategory('Limits');
    fireEvent.change(editField('Retry factor'), { target: { value: '2.75' } });
    write.mockResolvedValue(snapshot({ values: { ...snapshot().values, ratio: 2.75 } }));
    saveField('Retry factor');
    await waitFor(() =>
      expect(write).toHaveBeenLastCalledWith({ ratio: 2.75 }, expect.any(AbortSignal)),
    );
    await screen.findByText('Limits settings saved.');
    selectCategory('Site');
    fireEvent.change(editField('Product name'), { target: { value: '' } });
    write.mockResolvedValue(snapshot({ values: { ...snapshot().values, title: '' } }));
    saveField('Product name');
    await waitFor(() =>
      expect(write).toHaveBeenLastCalledWith({ title: '' }, expect.any(AbortSignal)),
    );
  });

  it('validates integer bounds before saving', async () => {
    render(<ApplicationSettings />);
    await loadedField('Product name');
    selectCategory('Limits');
    const input = editField('Files per request');
    for (const [raw, error] of [
      ['1.5', 'Enter a whole number.'],
      ['0', 'Enter a value of at least 1.'],
      ['101', 'Enter a value no greater than 100.'],
      ['', 'Enter a number.'],
    ]) {
      fireEvent.change(input, { target: { value: raw } });
      saveField('Files per request');
      expect(screen.getByRole('alert')).toHaveTextContent(error);
    }
    expect(write).not.toHaveBeenCalled();
  });

  it('writes multiline secrets once and clears their draft after saving', async () => {
    write.mockResolvedValue(snapshot());
    render(<ApplicationSettings />);
    await loadedField('Product name');
    selectCategory('Connections');
    editField('Service token');
    fireEvent.click(editField('Use multiple lines for Service token'));
    const input = editField('Service token');
    const secret = 'private-key-line-1\nprivate-key-line-2';
    fireEvent.change(input, { target: { value: secret } });
    saveField('Service token');
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith({ token: secret }, expect.any(AbortSignal)),
    );
    await screen.findByText('Connections settings saved.');
    expect(screen.getByLabelText('Service token')).toHaveTextContent('Configured');
    expect(screen.queryByText(secret)).not.toBeInTheDocument();
  });

  it('preserves drafts, multiline controls, and field errors while saving another category', async () => {
    write.mockResolvedValue(snapshot({ values: { ...snapshot().values, enabled: false } }));
    render(<ApplicationSettings />);
    fireEvent.change(await loadedField('Product name'), {
      target: { value: 'Unsaved name' },
    });
    selectCategory('Connections');
    editField('Service token');
    fireEvent.click(screen.getByRole('checkbox', { name: 'Use multiple lines for Service token' }));
    const secretInput = editField('Service token');
    const secret = 'private-key-line-1\nprivate-key-line-2';
    fireEvent.change(secretInput, { target: { value: secret } });
    expect(secretInput.tagName).toBe('TEXTAREA');

    selectCategory('Limits');
    fireEvent.change(editField('Files per request'), {
      target: { value: '101' },
    });
    saveField('Files per request');
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a value no greater than 100.');

    selectCategory('Access');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.change(editField('Registration'), {
      target: { value: 'false' },
    });
    saveField('Registration');
    await screen.findByText('Access settings saved.');
    expect(write).toHaveBeenCalledExactlyOnceWith({ enabled: false }, expect.any(AbortSignal));

    selectCategory('Limits');
    expect(editField('Files per request')).toHaveValue(101);
    expect(editField('Files per request')).toHaveAttribute('aria-invalid', 'true');
    expect(screen.getByRole('alert')).toHaveTextContent('Enter a value no greater than 100.');
    selectCategory('Site');
    expect(editField('Product name')).toHaveValue('Unsaved name');
    selectCategory('Connections');
    editField('Service token');
    expect(editField('Service token')).toBe(secretInput);
    expect(secretInput).toHaveValue(secret);
    expect(
      screen.getByRole('checkbox', { name: 'Use multiple lines for Service token' }),
    ).toBeChecked();
    expect(reviewButton('Service token')).toBeEnabled();
  });

  it('resets to the built-in default using null and preserves drafts in other rows', async () => {
    write.mockResolvedValue(snapshot());
    render(<ApplicationSettings />);
    fireEvent.change(await loadedField('Product name'), {
      target: { value: 'Unsaved name' },
    });
    selectCategory('Limits');
    fireEvent.change(editField('Files per request'), { target: { value: '70' } });
    resetField('Files per request');
    await waitFor(() =>
      expect(write).toHaveBeenCalledWith({ limit: null }, expect.any(AbortSignal)),
    );
    await screen.findByText('Files per request restored to its default.');
    expect(editField('Files per request')).toHaveValue(50);
    selectCategory('Site');
    expect(editField('Product name')).toHaveValue('Unsaved name');
  });

  it('keeps edits during refresh while updating pristine fields', async () => {
    render(<ApplicationSettings />);
    fireEvent.change(await loadedField('Product name'), {
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
    await waitFor(() => expect(editField('Files per request')).toHaveValue(80));
    expect(screen.getByRole('button', { name: 'Limits' })).toHaveAttribute('aria-pressed', 'true');
    selectCategory('Site');
    expect(editField('Product name')).toHaveValue('My draft');
  });

  it('ignores a read started before a save completes', async () => {
    const pending = deferred<AgentAdminSettings>();
    render(<ApplicationSettings />);
    await loadedField('Product name');
    read.mockReturnValueOnce(pending.promise);
    fireEvent.click(screen.getByRole('button', { name: 'Refresh settings' }));
    const readSignal = read.mock.calls[1][0]!;
    write.mockResolvedValue(
      snapshot({ revision: 2, values: { ...snapshot().values, title: 'Saved name' } }),
    );
    fireEvent.change(editField('Product name'), { target: { value: 'Saved name' } });
    saveField('Product name');
    await screen.findByText('Site settings saved.');
    expect(readSignal.aborted).toBe(true);
    await act(async () => pending.resolve(snapshot()));
    expect(editField('Product name')).toHaveValue('Saved name');
  });

  it('retries an initial failure and preserves edits after a failed write', async () => {
    read.mockRejectedValueOnce(new Error('Settings temporarily unavailable'));
    render(<ApplicationSettings />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Settings temporarily unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry application settings' }));
    fireEvent.change(await loadedField('Product name'), {
      target: { value: 'My draft' },
    });
    write.mockRejectedValue(new Error('Write failed'));
    saveField('Product name');
    expect(await within(screen.getByRole('dialog')).findByRole('alert')).toHaveTextContent(
      'Write failed',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Back to editing' }));
    expect(editField('Product name')).toHaveValue('My draft');
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
    fireEvent.change(await loadedField('Product name'), {
      target: { value: 'My draft' },
    });
    saveField('Product name');
    const signal = write.mock.calls[0][1]!;
    view.unmount();
    expect(signal.aborted).toBe(true);
    await act(async () => pending.resolve(snapshot()));
  });
});
