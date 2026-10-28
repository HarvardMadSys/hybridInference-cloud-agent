// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import * as api from '@/lib/api/modelConnections';
import { APIError } from '@/lib/utils/errors';
import { ModelConnectionsSettings } from './ModelConnectionsSettings';

// The calls are mocked; the error helpers and the vocabulary are the real ones.
vi.mock('@/lib/api/modelConnections', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/modelConnections')>()),
  listModelConnections: vi.fn(),
  createModelConnection: vi.fn(),
  updateModelConnection: vi.fn(),
  deleteModelConnection: vi.fn(),
  discoverConnectionModels: vi.fn(),
}));

const EVERY = ['claude-code', 'codex', 'kilo', 'opencode', 'pi'];
const AGENT_NAMES = 'Claude Code, Codex, Kilo Code, OpenCode, pi';

/** Chat Completions verified for `model`; Codex and Claude Code translated. */
function ready(model: string, runtimes = EVERY): api.ModelStatus {
  return {
    model,
    status: 'ready',
    category: null,
    message: '',
    runtimes,
    paths: [
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

function refused(
  model: string,
  message = 'The model did not call the tool.',
  category = 'no_tool_call',
): api.ModelStatus {
  return {
    ...ready(model),
    status: 'unsupported',
    category,
    message,
    runtimes: [],
    paths: [],
  };
}

function refusal(...models: api.ModelStatus[]) {
  return new APIError(api.VERIFICATION_FAILED, 'Nothing was saved.', 422, { models });
}

const connection: api.ModelConnection = {
  id: 'mconn_01234567890123456789012345678901',
  name: 'My provider',
  base_url: 'https://api.example.com/custom/v1',
  models: ['shared-model', 'another-model'],
  enabled: true,
  has_api_key: true,
  runtimes: EVERY,
  model_status: [ready('shared-model'), ready('another-model')],
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

function region() {
  return screen.getByRole('region', { name: /Personal model connections|个人模型连接/ });
}

function fillNew(models: string[]) {
  fireEvent.change(screen.getByLabelText('Connection name'), {
    target: { value: 'Second provider' },
  });
  fireEvent.change(screen.getByLabelText('Base URL'), {
    target: { value: 'https://api.example.com/custom/v1' },
  });
  fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'private-provider-key' } });
  const input = screen.getByLabelText('Model IDs');
  models.forEach((model, index) => {
    fireEvent.change(input, { target: { value: model } });
    // The last one stays typed: it counts when the form is saved.
    if (index < models.length - 1) fireEvent.keyDown(input, { key: 'Enter' });
  });
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  vi.mocked(api.listModelConnections).mockResolvedValue({
    configured: true,
    enabled: true,
    checks_per_hour: 20,
    max_models: 8,
    connections: [connection],
  });
});
afterEach(cleanup);

describe('adding a connection', () => {
  it('asks for a name, Base URL, key and model IDs only, with one primary action', async () => {
    vi.mocked(api.createModelConnection).mockResolvedValue({
      ...connection,
      id: 'new',
      name: 'Second provider',
    });
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
    const form = region().querySelector('form')!;
    // No protocol control, radio, checkbox or multi-line box anywhere.
    expect(within(form).queryByRole('checkbox')).not.toBeInTheDocument();
    expect(within(form).queryByRole('radio')).not.toBeInTheDocument();
    expect(form.querySelector('textarea, select, datalist')).toBeNull();
    expect(form).not.toHaveTextContent(/Chat Completions|Responses|Anthropic Messages|Advanced/);
    expect(screen.getByLabelText('API key')).toHaveAttribute('type', 'password');
    // The actions, in order: the primary one first, then Cancel.
    expect(
      within(form)
        .getAllByRole('button')
        .filter((button) => button.getAttribute('type') !== null)
        .map((button) => button.textContent),
    ).toEqual(['Add', 'Verify and save', 'Cancel']);
    expect(within(form).getByText(/may bill/)).toBeVisible();

    fillNew(['shared-model', 'shared-model', 'another-model']);
    expect(within(form).getByRole('list', { name: 'Models to save' })).toHaveTextContent(
      'shared-model',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
    await waitFor(() =>
      expect(api.createModelConnection).toHaveBeenCalledWith({
        name: 'Second provider',
        base_url: connection.base_url,
        api_key: 'private-provider-key',
        models: ['shared-model', 'another-model'],
      }),
    );
    const card = await screen.findByRole('article', { name: 'Second provider' });
    expect(within(card).getAllByText(`Works with ${AGENT_NAMES}`)).toHaveLength(2);
    expect(screen.queryByLabelText('API key')).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue('private-provider-key')).not.toBeInTheDocument();
  });

  it('keeps every input after a refused verification, marks the model and adds no card', async () => {
    vi.mocked(api.createModelConnection).mockRejectedValue(
      refusal(ready('good-model'), refused('bad-model')),
    );
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
    fillNew(['good-model', 'bad-model']);
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/Not saved/);
    expect(screen.getByLabelText('Connection name')).toHaveValue('Second provider');
    expect(screen.getByLabelText('Base URL')).toHaveValue('https://api.example.com/custom/v1');
    expect(screen.getByLabelText('API key')).toHaveValue('private-provider-key');
    const chips = screen.getByRole('list', { name: 'Models to save' });
    expect(chips).toHaveTextContent(
      'Not usable: The model did not call the offered tool exactly once.',
    );
    expect(chips).toHaveTextContent(`Works with ${AGENT_NAMES}`);
    expect(screen.queryByRole('article', { name: 'Second provider' })).not.toBeInTheDocument();

    // Removing the refused model clears the stale results; retrying saves the rest.
    vi.mocked(api.createModelConnection).mockResolvedValue({
      ...connection,
      id: 'new',
      name: 'Second provider',
      models: ['good-model'],
      model_status: [ready('good-model')],
    });
    fireEvent.click(screen.getByRole('button', { name: 'Remove bad-model' }));
    expect(chips).not.toHaveTextContent('Not usable');
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
    await waitFor(() =>
      expect(api.createModelConnection).toHaveBeenLastCalledWith(
        expect.objectContaining({ models: ['good-model'], api_key: 'private-provider-key' }),
      ),
    );
    expect(await screen.findByRole('article', { name: 'Second provider' })).toBeVisible();
  });

  it.each([
    ['model_connection_check_limit', 429, /Too many verifications/],
    ['model_connection_name_taken', 409, /already have a connection/],
  ])('says why %s refused it and keeps the form', async (code, status, message) => {
    vi.mocked(api.createModelConnection).mockRejectedValue(
      new APIError(code, 'upstream echoed private-provider-key', status),
    );
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
    fillNew(['m1']);
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(screen.queryByText(/upstream echoed/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('API key')).toHaveValue('private-provider-key');
  });

  it('adds model IDs from the keyboard and caps them at the server limit', async () => {
    vi.mocked(api.listModelConnections).mockResolvedValue({
      configured: true,
      enabled: true,
      max_models: 2,
      connections: [],
    });
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
    const input = screen.getByLabelText('Model IDs');
    const longId = `provider/${'very-long-model-name-'.repeat(8)}v1`;
    fireEvent.change(input, { target: { value: longId } });
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.change(input, { target: { value: 'second' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(screen.getByRole('list', { name: 'Models to save' })).toHaveTextContent(longId);
    expect(input).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: `Remove ${longId}` }));
    expect(input).toBeEnabled();
  });
});

describe('editing a connection', () => {
  it('saves a rename alone without verifying, so the key and processes stay', async () => {
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
    expect(screen.queryByText(/may bill/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, { name: 'Renamed' }),
    );
    expect(await screen.findByRole('article', { name: 'Renamed' })).toBeVisible();
  });

  it('saves nothing when nothing changed, and removing a model needs no verification', async () => {
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(screen.queryByLabelText('API key')).not.toBeInTheDocument());
    expect(api.updateModelConnection).not.toHaveBeenCalled();

    vi.mocked(api.updateModelConnection).mockResolvedValue({
      ...connection,
      models: ['shared-model'],
      model_status: [ready('shared-model')],
      updated_at: 'later',
    });
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: 'Remove another-model' }));
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, {
        models: ['shared-model'],
      }),
    );
  });

  it('verifies a new key before saving it, and a refusal keeps the old card as it was', async () => {
    vi.mocked(api.updateModelConnection).mockRejectedValue(
      refusal(
        refused('shared-model', 'The provider refused the key.', 'auth'),
        ready('another-model'),
      ),
    );
    renderSettings();
    const card = await screen.findByRole('article', { name: connection.name });
    fireEvent.click(within(card).getByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'replacement-key' } });
    expect(screen.getByText(/may bill/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));

    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, {
        api_key: 'replacement-key',
      }),
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(/Not saved/);
    expect(screen.getByLabelText('API key')).toHaveValue('replacement-key');
    expect(screen.getByRole('list', { name: 'Models to save' })).toHaveTextContent(
      'Not usable: The provider refused the API key (401/403).',
    );
    // The saved connection is untouched until a save succeeds.
    expect(within(card).getAllByText(`Works with ${AGENT_NAMES}`)).toHaveLength(2);

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(screen.queryByLabelText('API key')).not.toBeInTheDocument();
    expect(screen.getByRole('article', { name: connection.name })).toBeVisible();
  });

  it('adds a model through a verified save', async () => {
    vi.mocked(api.updateModelConnection).mockResolvedValue({
      ...connection,
      models: [...connection.models, 'third-model'],
      model_status: [...connection.model_status, ready('third-model', ['codex'])],
      updated_at: 'later',
    });
    renderSettings();
    fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
    fireEvent.change(screen.getByLabelText('Model IDs'), { target: { value: 'third-model' } });
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, {
        models: ['shared-model', 'another-model', 'third-model'],
      }),
    );
    const card = await screen.findByRole('article', { name: connection.name });
    expect(within(card).getByText('Works with Codex')).toBeVisible();
  });
});

describe('a saved connection', () => {
  it('shows each model and its agents, with protocols only in collapsed details', async () => {
    renderSettings();
    const card = await screen.findByRole('article', { name: connection.name });
    expect(within(card).queryByRole('button', { name: /^Verify/ })).not.toBeInTheDocument();
    const details = within(card).getByText('Technical details', { selector: 'summary' });
    for (const path of within(card).getAllByText('OpenCode: Chat Completions')) {
      expect(path).not.toBeVisible();
    }
    fireEvent.click(details);
    expect(within(card).getAllByText('OpenCode: Chat Completions')[0]).toBeVisible();
    expect(
      within(card).getAllByText('Codex: Responses, translated to Chat Completions')[0],
    ).toBeVisible();
    expect(within(card).getAllByText('Chat Completions: works')[0]).toBeVisible();
  });

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

  it('adds discovered IDs through a verified save and reports a refused one', async () => {
    vi.mocked(api.discoverConnectionModels).mockResolvedValue(['shared-model', 'third-model']);
    vi.mocked(api.updateModelConnection).mockRejectedValue(
      refusal(refused('third-model', 'The provider has no such model.', 'endpoint_missing')),
    );
    renderSettings();
    await screen.findByRole('article', { name: connection.name });
    fireEvent.click(screen.getByText('Discover model IDs', { selector: 'summary' }));
    fireEvent.click(screen.getByRole('button', { name: 'Discover model IDs' }));
    fireEvent.click(await screen.findByRole('checkbox', { name: 'third-model' }));
    fireEvent.click(screen.getByRole('button', { name: 'Verify and add selected' }));
    await waitFor(() =>
      expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, {
        models: [...connection.models, 'third-model'],
      }),
    );
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'third-model: The provider has no such endpoint or model (404). Check the Base URL, the model id, and that the provider offers this protocol.',
    );
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

  it('reads in Chinese with one primary action and no API names', async () => {
    renderSettings('zh-CN');
    fireEvent.click(await screen.findByRole('button', { name: '添加连接' }));
    expect(screen.getByLabelText('模型 ID')).toBeVisible();
    expect(screen.getByRole('button', { name: '验证并保存' })).toBeVisible();
    expect(screen.getByText(/服务商可能对此计费/)).toBeVisible();
    const form = region().querySelector('form')!;
    expect(form).not.toHaveTextContent(/Chat Completions|Responses|Anthropic Messages/);
    const card = screen.getByRole('article', { name: connection.name });
    expect(within(card).getAllByText(`可用于 ${AGENT_NAMES}`)).toHaveLength(2);
  });
});

it('reports malformed URL and model IDs beside the fields without calling the provider', async () => {
  renderSettings();
  fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
  fillNew(['model with spaces']);
  fireEvent.change(screen.getByLabelText('Base URL'), {
    target: { value: 'http://provider.example/v1' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
  expect(screen.getByLabelText('Base URL')).toHaveAttribute('aria-invalid', 'true');
  expect(screen.getByLabelText('Model IDs')).toHaveAttribute('aria-invalid', 'true');
  expect(screen.getByText(/Enter an HTTPS Base URL/)).toBeVisible();
  expect(screen.getByText(/Use the exact model ID/)).toBeVisible();
  expect(api.createModelConnection).not.toHaveBeenCalled();
  expect(screen.getByLabelText('API key')).toHaveValue('private-provider-key');
});

it('shows a Chinese reason and readable agent names after a mixed verification refusal', async () => {
  vi.mocked(api.createModelConnection).mockRejectedValue(
    refusal(ready('good-model'), refused('bad-model')),
  );
  renderSettings('zh-CN');
  fireEvent.click(await screen.findByRole('button', { name: '添加连接' }));
  fireEvent.change(screen.getByLabelText('连接名称'), { target: { value: '我的服务' } });
  fireEvent.change(screen.getByLabelText('Base URL'), {
    target: { value: 'https://provider.example/v1' },
  });
  fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'fixture-key' } });
  const input = screen.getByLabelText('模型 ID');
  fireEvent.change(input, { target: { value: 'good-model' } });
  fireEvent.keyDown(input, { key: 'Enter' });
  fireEvent.change(input, { target: { value: 'bad-model' } });
  fireEvent.click(screen.getByRole('button', { name: '验证并保存' }));
  expect(await screen.findByText('无法使用：模型未按要求调用工具。')).toBeVisible();
  expect(screen.getByRole('list', { name: '要保存的模型' })).toHaveTextContent(
    'Claude Code, Codex, Kilo Code, OpenCode, pi',
  );
  expect(screen.queryByText('The model did not call the tool.')).not.toBeInTheDocument();
});
