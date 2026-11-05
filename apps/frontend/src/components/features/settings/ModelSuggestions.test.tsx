// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import * as api from '@/lib/api/modelConnections';
import { APIError } from '@/lib/utils/errors';
import { ModelConnectionsSettings } from './ModelConnectionsSettings';

// Model IDs can be picked from the provider's own list, in the add and the
// edit form alike. Loading the list saves nothing and proves nothing: a
// picked ID is verified with the form, and only an answer for the exact
// Base URL and key the form still holds is ever shown.

vi.mock('@/lib/api/modelConnections', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/modelConnections')>()),
  listModelConnections: vi.fn(),
  createModelConnection: vi.fn(),
  updateModelConnection: vi.fn(),
  deleteModelConnection: vi.fn(),
  discoverModels: vi.fn(),
}));

const LOAD = 'Load models from provider';
const URL = 'https://provider.example/v1';
const KEY = 'fixture-key-not-a-credential';

const ready = (model: string): api.ModelStatus => ({
  model,
  status: 'ready',
  category: null,
  message: '',
  runtimes: ['codex'],
  paths: [],
  protocols: {},
  checked_at: '2026-10-28T00:00:00Z',
});
const connection: api.ModelConnection = {
  id: `mconn_${'a'.repeat(32)}`,
  name: 'Saved provider',
  base_url: URL,
  models: ['saved-model'],
  enabled: true,
  has_api_key: true,
  runtimes: ['codex'],
  model_status: [ready('saved-model')],
  created_at: '2026-10-27T00:00:00Z',
  updated_at: '2026-10-27T00:00:00Z',
};

/** A listing the test answers when it chooses, with the signal it was sent. */
function deferred() {
  const calls: {
    request: api.ModelListRequest;
    signal: AbortSignal;
    resolve: (models: string[]) => void;
    reject: (cause: unknown) => void;
  }[] = [];
  vi.mocked(api.discoverModels).mockImplementation(
    (request, signal) =>
      new Promise((resolve, reject) => {
        calls.push({ request, signal: signal!, resolve, reject });
      }),
  );
  return calls;
}

function renderSettings(locale: 'en' | 'zh-CN' = 'en', connections = [connection]) {
  localStorage.setItem(LOCALE_STORAGE_KEY, locale);
  vi.mocked(api.listModelConnections).mockResolvedValue({
    configured: true,
    enabled: true,
    checks_per_hour: 20,
    max_models: 8,
    connections,
  });
  return render(
    <LocaleProvider>
      <ModelConnectionsSettings />
    </LocaleProvider>,
  );
}

function type(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

async function openNew() {
  renderSettings();
  fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
  type('Connection name', 'New provider');
}

async function openEdit() {
  renderSettings();
  fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
}

function suggestionList() {
  return screen.getByRole('list', { name: 'Models your provider lists' });
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
});
afterEach(cleanup);

describe('a new connection', () => {
  it('loads suggestions from the form’s URL and key, and saves only with verification', async () => {
    vi.mocked(api.discoverModels).mockResolvedValue(['gpt-4.1', 'gpt-4.1-mini', 'o3']);
    vi.mocked(api.createModelConnection).mockResolvedValue({
      ...connection,
      id: 'new',
      name: 'New provider',
    });
    await openNew();
    const load = screen.getByRole('button', { name: LOAD });
    expect(load).toBeDisabled();
    expect(screen.getByText(/Enter the Base URL and API key to load/)).toBeVisible();
    type('Base URL', `${URL}/`);
    expect(load).toBeDisabled();
    type('API key', ` ${KEY} `);
    expect(load).toBeEnabled();
    expect(load).toHaveAccessibleDescription(/hourly checks.*Nothing is saved/);

    fireEvent.click(load);
    expect(api.discoverModels).toHaveBeenCalledWith(
      { base_url: `${URL}/`, api_key: KEY },
      expect.any(AbortSignal),
    );
    expect(await screen.findByText(/Your provider lists 3/)).toBeVisible();
    // Typing in the model line narrows the list; picking adds the ID, nothing else.
    type('Model IDs', 'GPT-4.1');
    expect(within(suggestionList()).getAllByRole('button')).toHaveLength(2);
    fireEvent.click(screen.getByRole('button', { name: 'Add gpt-4.1-mini' }));
    expect(screen.getByLabelText('Model IDs')).toHaveValue('');
    expect(screen.getByRole('list', { name: 'Models to save' })).toHaveTextContent('gpt-4.1-mini');
    expect(within(suggestionList()).queryByText('gpt-4.1-mini')).not.toBeInTheDocument();
    // A listing creates nothing, and any other ID can still be typed by hand.
    expect(api.createModelConnection).not.toHaveBeenCalled();
    type('Model IDs', 'my-private-model');
    expect(screen.getByText(/No listed model matches/)).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
    await waitFor(() =>
      expect(api.createModelConnection).toHaveBeenCalledWith({
        name: 'New provider',
        base_url: `${URL}/`,
        api_key: KEY,
        models: ['gpt-4.1-mini', 'my-private-model'],
      }),
    );
    expect(api.discoverModels).toHaveBeenCalledTimes(1);
  });

  it('stops offering picks at the model limit and never adds every listed model', async () => {
    const listed = Array.from({ length: 12 }, (_, index) => `model-${index}`);
    vi.mocked(api.discoverModels).mockResolvedValue(listed);
    await openNew();
    type('Base URL', URL);
    type('API key', KEY);
    fireEvent.click(screen.getByRole('button', { name: LOAD }));
    await screen.findByText(/Your provider lists 12/);
    expect(screen.queryByRole('list', { name: 'Models to save' })).not.toBeInTheDocument();
    for (const model of listed.slice(0, 8)) {
      fireEvent.click(screen.getByRole('button', { name: `Add ${model}` }));
    }
    expect(screen.getByRole('list', { name: 'Models to save' }).children).toHaveLength(8);
    expect(screen.getByRole('button', { name: 'Add model-8' })).toBeDisabled();
    expect(screen.getByText('Up to 8 models per connection.')).toBeVisible();
  });

  it('explains when every listed model is added and still allows manual entry', async () => {
    vi.mocked(api.discoverModels).mockResolvedValue(['only-model']);
    await openNew();
    type('Base URL', URL);
    type('API key', KEY);
    fireEvent.click(screen.getByRole('button', { name: LOAD }));
    fireEvent.click(await screen.findByRole('button', { name: 'Add only-model' }));
    expect(screen.getByText(/All listed models have been added/)).toBeVisible();
    type('Model IDs', 'other-model');
    expect(screen.getByText(/No listed model matches/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Verify and save' })).toBeEnabled();
  });

  it('explains a failed or empty listing and keeps every input usable', async () => {
    vi.mocked(api.discoverModels).mockRejectedValueOnce(
      new APIError('model_connection_endpoint_missing', 'The provider has no such endpoint.', 422),
    );
    await openNew();
    type('Base URL', URL);
    type('API key', KEY);
    const input = screen.getByLabelText('Model IDs');
    fireEvent.change(input, { target: { value: 'kept-model' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    fireEvent.change(input, { target: { value: 'half-typed' } });
    fireEvent.click(screen.getByRole('button', { name: LOAD }));
    expect(
      await screen.findByText(
        'Your provider doesn’t offer a model list. Type the model ID instead.',
      ),
    ).toBeVisible();
    expect(screen.queryByText('The provider has no such endpoint.')).not.toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    expect(input).toHaveValue('half-typed');
    expect(input).toBeEnabled();
    expect(screen.getByRole('list', { name: 'Models to save' })).toHaveTextContent('kept-model');
    expect(screen.getByRole('button', { name: 'Verify and save' })).toBeEnabled();

    vi.mocked(api.discoverModels).mockResolvedValueOnce([]);
    fireEvent.click(screen.getByRole('button', { name: LOAD }));
    expect(await screen.findByText(/Your provider listed no models/)).toBeVisible();
    expect(api.createModelConnection).not.toHaveBeenCalled();
  });

  it.each([
    ['model_connection_auth', /refused this API key/],
    ['model_connection_unreachable', /Couldn’t reach your provider/],
    ['model_connection_blocked_address', /Couldn’t reach your provider/],
    ['model_connection_rate_limited', /rate limiting this key/],
    ['model_connection_check_limit', /Too many checks in the last hour/],
    ['NETWORK_ERROR', /Couldn’t load the model list/],
  ])('maps %s to a short localized reason', async (code, message) => {
    vi.mocked(api.discoverModels).mockRejectedValue(new APIError(code, 'upstream text', 422));
    await openNew();
    type('Base URL', URL);
    type('API key', KEY);
    fireEvent.click(screen.getByRole('button', { name: LOAD }));
    expect(await screen.findByText(message)).toBeVisible();
    expect(screen.queryByText(/upstream text/)).not.toBeInTheDocument();
  });

  it('reads in Chinese without naming any API', async () => {
    vi.mocked(api.discoverModels).mockRejectedValue(
      new APIError('model_connection_endpoint_missing', 'x', 422),
    );
    renderSettings('zh-CN');
    fireEvent.click(await screen.findByRole('button', { name: '添加连接' }));
    const load = screen.getByRole('button', { name: '从服务商获取模型' });
    expect(screen.getByText(/填写 Base URL 和 API key 后即可获取/)).toBeVisible();
    type('Base URL', URL);
    type('API key', KEY);
    fireEvent.click(load);
    expect(await screen.findByText('服务商未提供模型列表，请手动输入模型 ID。')).toBeVisible();
    const form = screen.getByRole('region', { name: '个人模型连接' }).querySelector('form')!;
    expect(form).not.toHaveTextContent(/Chat Completions|Responses|Anthropic|protocol|协议/);
  });
});

describe('editing a connection', () => {
  it('uses the saved key only while the Base URL is the saved one', async () => {
    vi.mocked(api.discoverModels).mockResolvedValue(['listed-model']);
    await openEdit();
    const load = screen.getByRole('button', { name: LOAD });
    fireEvent.click(load);
    expect(api.discoverModels).toHaveBeenLastCalledWith(
      { base_url: URL, connection_id: connection.id },
      expect.any(AbortSignal),
    );
    await screen.findByRole('button', { name: 'Add listed-model' });

    // A changed URL never gets the saved key: the form asks for one instead.
    type('Base URL', 'https://elsewhere.example/v1');
    expect(screen.queryByRole('button', { name: 'Add listed-model' })).not.toBeInTheDocument();
    expect(load).toBeDisabled();
    expect(
      screen.getByText('Enter the API key to load models from the changed Base URL.'),
    ).toHaveAttribute('role', 'status');
    expect(api.discoverModels).toHaveBeenCalledTimes(1);

    // A new key is used as entered, at the URL entered, and never swapped for the saved one.
    type('API key', 'rotated-fixture-key');
    fireEvent.click(load);
    expect(api.discoverModels).toHaveBeenLastCalledWith(
      { base_url: 'https://elsewhere.example/v1', api_key: 'rotated-fixture-key' },
      expect.any(AbortSignal),
    );
    type('Base URL', `${URL}/`);
    fireEvent.click(load);
    expect(api.discoverModels).toHaveBeenLastCalledWith(
      { base_url: `${URL}/`, api_key: 'rotated-fixture-key' },
      expect.any(AbortSignal),
    );
    expect(api.updateModelConnection).not.toHaveBeenCalled();
  });

  it('explains when the server asks for a key at a changed address', async () => {
    vi.mocked(api.discoverModels).mockRejectedValue(
      new APIError('model_connection_key_required', 'x', 422),
    );
    await openEdit();
    fireEvent.click(screen.getByRole('button', { name: LOAD }));
    expect(await screen.findByText(/Enter the API key to load models/)).toBeVisible();
  });
});

describe('late answers', () => {
  it('drops an answer once the URL or key it was asked with has changed', async () => {
    const calls = deferred();
    await openNew();
    type('Base URL', URL);
    type('API key', KEY);
    fireEvent.click(screen.getByRole('button', { name: LOAD }));
    expect(screen.getByRole('button', { name: 'Loading…' })).toBeDisabled();
    // Fetching never blocks typing.
    expect(screen.getByLabelText('API key')).toBeEnabled();
    type('API key', `${KEY}-2`);
    expect(calls[0].signal.aborted).toBe(true);
    await act(async () => calls[0].resolve(['stale-model']));
    expect(screen.queryByText('stale-model')).not.toBeInTheDocument();
    expect(screen.queryByText(/Your provider lists/)).not.toBeInTheDocument();

    // Back to the same key: still not shown, since it was asked for an earlier form.
    type('API key', KEY);
    expect(screen.queryByText('stale-model')).not.toBeInTheDocument();
  });

  it('shows only the newest of overlapping requests', async () => {
    const calls = deferred();
    await openEdit();
    type('API key', 'first-fixture-key');
    fireEvent.click(screen.getByRole('button', { name: LOAD }));
    type('API key', 'second-fixture-key');
    fireEvent.click(screen.getByRole('button', { name: LOAD }));
    expect(calls).toHaveLength(2);
    await act(async () => calls[1].resolve(['newest-model']));
    await act(async () => calls[0].resolve(['older-model']));
    expect(screen.getByRole('button', { name: 'Add newest-model' })).toBeVisible();
    expect(screen.queryByText('older-model')).not.toBeInTheDocument();
    // A late failure of the older one changes nothing either.
    await act(async () => calls[0].reject(new APIError('model_connection_auth', 'x', 422)));
    expect(screen.queryByText(/refused this API key/)).not.toBeInTheDocument();
  });

  it('aborts on Cancel and on save, and a late answer never reaches the page', async () => {
    const calls = deferred();
    await openEdit();
    fireEvent.click(screen.getByRole('button', { name: LOAD }));
    // Cancel stays available while loading, and leaving aborts the request.
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(calls[0].signal.aborted).toBe(true);
    await act(async () => calls[0].resolve(['after-cancel']));
    expect(screen.queryByText('after-cancel')).not.toBeInTheDocument();

    vi.mocked(api.updateModelConnection).mockRejectedValue(
      new APIError('model_connection_check_limit', 'x', 429),
    );
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
    fireEvent.click(screen.getByRole('button', { name: LOAD }));
    type('Model IDs', 'typed-model');
    fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
    expect(calls[1].signal.aborted).toBe(true);
    await screen.findByText(/Too many verifications/);
    await act(async () => calls[1].resolve(['after-save']));
    expect(screen.queryByText('after-save')).not.toBeInTheDocument();
    // The refused save kept the draft and the saved models as they were.
    expect(screen.getByRole('list', { name: 'Models to save' })).toHaveTextContent(
      'saved-modeltyped-model',
    );
    expect(api.updateModelConnection).toHaveBeenCalledWith(connection.id, {
      models: ['saved-model', 'typed-model'],
    });
  });
});
