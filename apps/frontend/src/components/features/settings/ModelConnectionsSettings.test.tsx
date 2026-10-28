// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import * as api from '@/lib/api/modelConnections';
import { ModelConnectionsSettings } from './ModelConnectionsSettings';

// The calls are mocked; the protocol vocabulary and status helpers are the real ones.
vi.mock('@/lib/api/modelConnections', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/modelConnections')>()),
  listModelConnections: vi.fn(),
  createModelConnection: vi.fn(),
  updateModelConnection: vi.fn(),
  deleteModelConnection: vi.fn(),
  verifyModelConnection: vi.fn(),
  discoverConnectionModels: vi.fn(),
}));

function unverified(model: string): api.ModelStatus {
  return {
    model,
    status: 'unverified',
    category: null,
    message: '',
    runtimes: [],
    paths: [],
    protocols: {},
    checked_at: null,
  };
}

/** Chat Completions verified for `model`; Codex and Claude Code through translation. */
function chatReady(model: string): api.ModelStatus {
  return {
    model,
    status: 'ready',
    category: null,
    message: '',
    runtimes: ['claude-code', 'codex', 'kilo', 'opencode', 'pi'],
    paths: [
      {
        runtime: 'claude-code',
        mode: 'translated',
        client_protocol: 'anthropic_messages',
        upstream_protocol: 'chat_completions',
      },
      {
        runtime: 'codex',
        mode: 'translated',
        client_protocol: 'responses',
        upstream_protocol: 'chat_completions',
      },
      {
        runtime: 'opencode',
        mode: 'direct',
        client_protocol: 'chat_completions',
        upstream_protocol: 'chat_completions',
      },
    ],
    protocols: {
      chat_completions: {
        status: 'supported',
        category: 'supported',
        message: '',
        http_status: null,
      },
      responses: {
        status: 'unsupported',
        category: 'endpoint_missing',
        message: 'The provider has no such endpoint.',
        http_status: 404,
      },
    },
    checked_at: '2026-10-28T00:00:00Z',
  };
}

const connection: api.ModelConnection = {
  id: 'mconn_01234567890123456789012345678901',
  name: 'My provider',
  base_url: 'https://api.example.com/custom/v1',
  protocol_mode: 'auto',
  manual_protocols: [],
  models: ['shared-model', 'another-model'],
  enabled: true,
  has_api_key: true,
  runtimes: [],
  model_status: [unverified('shared-model'), unverified('another-model')],
  created_at: '2026-10-27T00:00:00Z',
  updated_at: '2026-10-27T00:00:00Z',
};

function renderSettings(locale: 'en' | 'zh-CN' = 'en') {
  localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  return render(
    <LocaleProvider>
      <ModelConnectionsSettings />
    </LocaleProvider>,
  );
}

function fillNew(models: string) {
  fireEvent.change(screen.getByLabelText('Connection name'), {
    target: { value: 'Second provider' },
  });
  fireEvent.change(screen.getByLabelText('Base URL'), {
    target: { value: 'https://api.example.com/custom/v1' },
  });
  fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'private-provider-key' } });
  fireEvent.change(screen.getByLabelText('Model IDs'), { target: { value: models } });
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  vi.mocked(api.listModelConnections).mockResolvedValue({
    configured: true,
    enabled: true,
    checks_per_hour: 20,
    connections: [connection],
  });
});
afterEach(cleanup);

describe('adding a connection', () => {
  it('asks only for a name, Base URL, key and models, and saving calls no provider', async () => {
    vi.mocked(api.createModelConnection).mockResolvedValue({
      ...connection,
      id: 'new',
      name: 'Second provider',
    });
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
    // No protocol is asked for on the ordinary path: the override is collapsed.
    for (const name of ['Chat Completions', 'Responses', 'Anthropic Messages']) {
      expect(screen.queryByRole('checkbox', { name })).not.toBeInTheDocument();
    }
    expect(screen.getByText('Advanced', { selector: 'summary' })).toBeVisible();
    expect(screen.queryByText(/OpenCode, Kilo, pi/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('API key')).toHaveAttribute('type', 'password');
    fillNew('shared-model\nshared-model\nanother-model');
    expect(screen.getAllByText(/Your provider may bill them/)[0]).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(api.createModelConnection).toHaveBeenCalledWith({
        name: 'Second provider',
        base_url: connection.base_url,
        api_key: 'private-provider-key',
        models: connection.models,
      }),
    );
    const card = await screen.findByRole('article', { name: 'Second provider' });
    expect(within(card).getAllByText('Not verified')).toHaveLength(2);
    expect(screen.queryByDisplayValue('private-provider-key')).not.toBeInTheDocument();
    expect(api.verifyModelConnection).not.toHaveBeenCalled();
    expect(api.discoverConnectionModels).not.toHaveBeenCalled();
  });

  it('"Save and verify" verifies at most three models, one at a time, and says how many remain', async () => {
    const models = ['m1', 'm2', 'm3', 'm4'];
    vi.mocked(api.createModelConnection).mockResolvedValue({
      ...connection,
      id: 'new',
      name: 'Second provider',
      models,
      model_status: models.map(unverified),
    });
    let inFlight = 0;
    vi.mocked(api.verifyModelConnection).mockImplementation(async (_id, model) => {
      inFlight += 1;
      expect(inFlight).toBe(1);
      await Promise.resolve();
      inFlight -= 1;
      return { ...chatReady(model), recorded: true };
    });
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
    fillNew(models.join('\n'));
    fireEvent.click(screen.getByRole('button', { name: 'Save and verify' }));
    const card = await screen.findByRole('article', { name: 'Second provider' });
    await waitFor(() => expect(within(card).getAllByText('Ready')).toHaveLength(3));
    expect(vi.mocked(api.verifyModelConnection).mock.calls).toEqual([
      ['new', 'm1'],
      ['new', 'm2'],
      ['new', 'm3'],
    ]);
    expect(within(card).getByText('Models not verified yet: 1.')).toBeVisible();
    expect(within(card).getByText('3 of 4 models ready')).toBeVisible();
    // The agents are named; how each reaches the model is not on the normal path.
    expect(
      within(card).getAllByText('Works with claude-code, codex, kilo, opencode, pi')[0],
    ).toBeVisible();
    expect(within(card).queryByText(/via Chat Completions/)).not.toBeInTheDocument();
    for (const path of within(card).getAllByText(
      /codex: Responses, translated to Chat Completions/,
    )) {
      expect(path).not.toBeVisible();
    }
  });

  it('declares APIs only through the advanced override, and never an empty declaration', async () => {
    vi.mocked(api.createModelConnection).mockResolvedValue({
      ...connection,
      id: 'new',
      name: 'Second provider',
      protocol_mode: 'manual',
      manual_protocols: ['anthropic_messages'],
    });
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
    fillNew('claude-sonnet-4-5');
    fireEvent.click(screen.getByText('Advanced', { selector: 'summary' }));
    expect(
      screen.getByRole('radio', { name: 'Detect automatically when verifying (recommended)' }),
    ).toBeChecked();
    fireEvent.click(screen.getByRole('radio', { name: 'Declare the APIs this endpoint offers' }));
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Anthropic Messages' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(api.createModelConnection).toHaveBeenCalledWith({
        name: 'Second provider',
        base_url: connection.base_url,
        api_key: 'private-provider-key',
        models: ['claude-sonnet-4-5'],
        protocols: ['anthropic_messages'],
      }),
    );
  });
});

describe('editing a connection', () => {
  it('sends only the name on a rename, so the key and verification stay', async () => {
    vi.mocked(api.updateModelConnection).mockResolvedValue({
      ...connection,
      name: 'Renamed',
      updated_at: 'later',
    });
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    expect(screen.getByLabelText('API key')).toHaveValue('');
    expect(screen.getByLabelText('API key')).not.toBeRequired();
    fireEvent.change(screen.getByLabelText('Connection name'), { target: { value: 'Renamed' } });
    // The same address typed with a trailing slash is not a change either.
    fireEvent.change(screen.getByLabelText('Base URL'), {
      target: { value: `${connection.base_url}/` },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, { name: 'Renamed' }),
    );
    expect(await screen.findByRole('article', { name: 'Renamed' })).toBeVisible();
  });

  it('saves nothing when nothing changed', async () => {
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.queryByLabelText('API key')).not.toBeInTheDocument());
    expect(api.updateModelConnection).not.toHaveBeenCalled();
  });

  it('sends an added model alone, and the new model stays unverified', async () => {
    vi.mocked(api.listModelConnections).mockResolvedValue({
      configured: true,
      enabled: true,
      connections: [
        { ...connection, model_status: [chatReady('shared-model'), chatReady('another-model')] },
      ],
    });
    vi.mocked(api.updateModelConnection).mockResolvedValue({
      ...connection,
      models: [...connection.models, 'third-model'],
      model_status: [
        chatReady('shared-model'),
        chatReady('another-model'),
        unverified('third-model'),
      ],
      updated_at: 'later',
    });
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Model IDs'), {
      target: { value: 'shared-model\nanother-model\nthird-model' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, {
        models: ['shared-model', 'another-model', 'third-model'],
      }),
    );
    const card = await screen.findByRole('article', { name: connection.name });
    expect(within(card).getAllByText('Ready')).toHaveLength(2);
    expect(within(card).getByText('Not verified')).toBeVisible();
    expect(within(card).getByText('Verify this model before tasks can use it.')).toBeVisible();
    expect(api.verifyModelConnection).not.toHaveBeenCalled();
  });

  it('sends an explicitly entered replacement key and clears the form on success', async () => {
    vi.mocked(api.updateModelConnection).mockResolvedValue({ ...connection, updated_at: 'later' });
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'replacement-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, {
        api_key: 'replacement-key',
      }),
    );
    await waitFor(() => expect(screen.queryByLabelText('API key')).not.toBeInTheDocument());
  });

  it('keeps the form for correction but never renders the raw failure or key', async () => {
    vi.mocked(api.updateModelConnection).mockRejectedValue(
      new Error('upstream echoed replacement-key'),
    );
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'replacement-key' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The request failed.');
    expect(screen.queryByText(/upstream echoed/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('API key')).toHaveAttribute('type', 'password');
  });
});

describe('verifying models', () => {
  it('verifies only the chosen model and shows mixed native and translated agents', async () => {
    vi.mocked(api.verifyModelConnection).mockResolvedValue({
      ...chatReady('another-model'),
      recorded: true,
    });
    renderSettings();
    const card = await screen.findByRole('article', { name: connection.name });
    // Opening the page spends no quota.
    expect(api.verifyModelConnection).not.toHaveBeenCalled();
    fireEvent.click(within(card).getByRole('button', { name: 'Verify another-model' }));
    await waitFor(() =>
      expect(api.verifyModelConnection).toHaveBeenCalledWith(connection.id, 'another-model'),
    );
    await within(card).findByText('Ready');
    expect(within(card).getByText('1 of 2 models ready')).toBeVisible();
    expect(within(card).getAllByText('Not verified')).toHaveLength(1);
    expect(api.verifyModelConnection).toHaveBeenCalledTimes(1);
    fireEvent.click(within(card).getByText('API details', { selector: 'summary' }));
    expect(within(card).getByText('Chat Completions: works')).toBeVisible();
    expect(within(card).getByText(/Responses: not available\./)).toBeVisible();
    expect(within(card).getByText('opencode: Chat Completions')).toBeVisible();
    expect(
      within(card).getByText('claude-code: Anthropic Messages, translated to Chat Completions'),
    ).toBeVisible();
  });

  it('keeps nothing from a verification the connection outran, and says so', async () => {
    vi.mocked(api.verifyModelConnection).mockResolvedValue({
      ...chatReady('shared-model'),
      recorded: false,
    });
    renderSettings();
    const card = await screen.findByRole('article', { name: connection.name });
    fireEvent.click(within(card).getByRole('button', { name: 'Verify shared-model' }));
    expect(await within(card).findByText(/changed while it was being verified/)).toBeVisible();
    expect(within(card).getAllByText('Not verified')).toHaveLength(2);
    expect(within(card).queryByText('Ready')).not.toBeInTheDocument();
  });

  it('tells a failed verification apart from a model no agent can use', async () => {
    vi.mocked(api.listModelConnections).mockResolvedValue({
      configured: true,
      enabled: true,
      connections: [
        {
          ...connection,
          model_status: [
            {
              ...unverified('shared-model'),
              status: 'failed',
              category: 'rate_limited',
              message: 'The provider rate limited the check. Try again later.',
              protocols: {
                chat_completions: {
                  status: 'failed',
                  category: 'rate_limited',
                  message: 'The provider rate limited the check. Try again later.',
                  http_status: 429,
                },
              },
            },
            {
              ...unverified('another-model'),
              status: 'unsupported',
              category: 'tool_call_missing',
              message: 'The model did not call the tool.',
            },
          ],
        },
      ],
    });
    renderSettings();
    const card = await screen.findByRole('article', { name: connection.name });
    expect(within(card).getByText('Couldn’t verify')).toBeVisible();
    expect(within(card).getAllByText(/rate limited the check/)[0]).toBeVisible();
    expect(within(card).getByText('Not usable for agents')).toBeVisible();
    expect(within(card).getByText('The model did not call the tool.')).toBeVisible();
    expect(within(card).getAllByRole('button', { name: /^Verify / })).toHaveLength(2);
    expect(within(card).getAllByText('Verify again')).toHaveLength(2);
  });

  it('cannot verify a disabled connection', async () => {
    vi.mocked(api.listModelConnections).mockResolvedValue({
      configured: true,
      enabled: true,
      connections: [{ ...connection, enabled: false }],
    });
    renderSettings();
    const card = await screen.findByRole('article', { name: connection.name });
    for (const button of within(card).getAllByRole('button', { name: /^Verify / })) {
      expect(button).toBeDisabled();
    }
  });

  it('marks a manual declaration as declared rather than verified', async () => {
    vi.mocked(api.listModelConnections).mockResolvedValue({
      configured: true,
      enabled: true,
      connections: [
        {
          ...connection,
          protocol_mode: 'manual',
          manual_protocols: ['chat_completions'],
          runtimes: ['opencode'],
          model_status: connection.models.map((model) => ({
            ...unverified(model),
            status: 'declared' as const,
            runtimes: ['opencode'],
          })),
        },
      ],
    });
    renderSettings();
    const card = await screen.findByRole('article', { name: connection.name });
    expect(within(card).getByText('APIs declared manually')).toBeVisible();
    expect(within(card).getAllByText('Declared')).toHaveLength(2);
    fireEvent.click(within(card).getByRole('button', { name: 'Edit' }));
    expect(
      screen.getByRole('radio', { name: 'Declare the APIs this endpoint offers' }),
    ).toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Chat Completions' })).toBeChecked();
  });
});

describe('connection lifecycle', () => {
  it('requires confirmation for disable and delete and does not claim provider revocation', async () => {
    vi.mocked(api.updateModelConnection).mockResolvedValue({
      ...connection,
      enabled: false,
      updated_at: 'later',
    });
    vi.mocked(api.deleteModelConnection).mockResolvedValue(undefined);
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Disable' }));
    expect(api.updateModelConnection).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, { enabled: false }),
    );
    await screen.findByRole('button', { name: 'Enable' });
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
    expect(screen.getByText(/does not revoke the key at your provider/)).toBeVisible();
    expect(api.deleteModelConnection).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    await waitFor(() => expect(api.deleteModelConnection).toHaveBeenCalledWith(connection.id));
    expect(await screen.findByText('No personal connections yet.')).toBeVisible();
  });

  it('adds only explicitly selected discovered IDs and does not verify them', async () => {
    vi.mocked(api.discoverConnectionModels).mockResolvedValue([
      'shared-model',
      'third-model',
      'fourth-model',
    ]);
    vi.mocked(api.updateModelConnection).mockResolvedValue({
      ...connection,
      models: [...connection.models, 'third-model'],
      model_status: [...connection.model_status, unverified('third-model')],
      updated_at: 'later',
    });
    renderSettings();
    await screen.findByRole('article', { name: connection.name });
    fireEvent.click(screen.getByText('Discover model IDs', { selector: 'summary' }));
    fireEvent.click(screen.getByRole('button', { name: 'Discover model IDs' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: 'third-model' }));
    fireEvent.click(screen.getByRole('button', { name: 'Add selected IDs' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, {
        models: [...connection.models, 'third-model'],
      }),
    );
    expect(api.verifyModelConnection).not.toHaveBeenCalled();
  });

  it('allows cleanup while the deployment gate is off but prevents new use', async () => {
    vi.mocked(api.listModelConnections).mockResolvedValue({
      configured: true,
      enabled: false,
      connections: [{ ...connection, enabled: false }],
    });
    renderSettings();
    expect(await screen.findByText(/not available on this deployment/)).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Add connection' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Edit' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Enable' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Delete' })).toBeEnabled();
  });

  it('reads in Chinese without naming any API on the normal path', async () => {
    renderSettings('zh-CN');
    fireEvent.click(await screen.findByRole('button', { name: '添加连接' }));
    expect(screen.getByLabelText('模型 ID')).toBeVisible();
    expect(screen.getByRole('button', { name: '保存并验证' })).toBeVisible();
    expect(screen.queryByRole('checkbox', { name: 'Chat Completions' })).not.toBeInTheDocument();
    const card = screen.getByRole('article', { name: connection.name });
    expect(within(card).getAllByText('未验证')).toHaveLength(2);
    expect(within(card).getByText('2 个模型中有 0 个可用')).toBeVisible();
  });
});
