// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import * as api from '@/lib/api/modelConnections';
import { APIError } from '@/lib/utils/errors';
import { ModelConnectionsSettings } from './ModelConnectionsSettings';

// Verification runs inside the save request, so every race here is a save
// answered late: after Cancel, a delete, leaving the page, or a newer edit.

vi.mock('@/lib/api/modelConnections', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/modelConnections')>()),
  listModelConnections: vi.fn(),
  createModelConnection: vi.fn(),
  updateModelConnection: vi.fn(),
  deleteModelConnection: vi.fn(),
  discoverConnectionModels: vi.fn(),
}));

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
  id: `mconn_${'e'.repeat(32)}`,
  name: 'My provider',
  base_url: 'https://old.example/v1',
  models: ['m1'],
  enabled: true,
  has_api_key: true,
  runtimes: ['codex'],
  model_status: [ready('m1')],
  created_at: '2026-10-28T00:00:00Z',
  updated_at: '2026-10-28T00:00:00Z',
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((done, fail) => {
    resolve = done;
    reject = fail;
  });
  return { promise, resolve, reject };
}

function mount() {
  return render(
    <LocaleProvider>
      <ModelConnectionsSettings />
    </LocaleProvider>,
  );
}

function editBaseUrl(value: string) {
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  fireEvent.change(screen.getByLabelText('Base URL'), { target: { value } });
  fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  localStorage.setItem(LOCALE_STORAGE_KEY, 'en');
  vi.mocked(api.listModelConnections).mockResolvedValue({
    configured: true,
    enabled: true,
    connections: [connection],
  });
});
afterEach(cleanup);

it('a create answered after Cancel adds only what the server re-read says exists', async () => {
  const save = deferred<api.ModelConnection>();
  vi.mocked(api.createModelConnection).mockReturnValue(save.promise);
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Add connection' }));
  fireEvent.change(screen.getByLabelText('Connection name'), { target: { value: 'Late' } });
  fireEvent.change(screen.getByLabelText('Base URL'), {
    target: { value: 'https://late.example/v1' },
  });
  fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'fixture-key' } });
  fireEvent.change(screen.getByLabelText('Model IDs'), { target: { value: 'm1' } });
  fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
  // Cancel stays usable while a slow provider is being verified.
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  expect(screen.queryByLabelText('API key')).not.toBeInTheDocument();
  expect(api.listModelConnections).toHaveBeenCalledTimes(1);

  // The server finished anyway; the page re-reads instead of trusting the late answer.
  vi.mocked(api.listModelConnections).mockResolvedValue({
    configured: true,
    enabled: true,
    connections: [connection],
  });
  await act(async () => {
    save.resolve({ ...connection, id: `mconn_${'f'.repeat(32)}`, name: 'Late' });
    await save.promise;
  });
  await waitFor(() => expect(api.listModelConnections).toHaveBeenCalledTimes(2));
  expect(screen.queryByRole('article', { name: 'Late' })).not.toBeInTheDocument();
  expect(screen.queryByLabelText('API key')).not.toBeInTheDocument();
});

it('a refused save answered after Cancel neither reopens the form nor shows its error', async () => {
  const save = deferred<api.ModelConnection>();
  vi.mocked(api.updateModelConnection).mockReturnValue(save.promise);
  mount();
  await screen.findByRole('article', { name: 'My provider' });
  editBaseUrl('https://new.example/v1');
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
  await act(async () => {
    save.reject(new APIError(api.VERIFICATION_FAILED, 'no', 422, { models: [] }));
    await save.promise.catch(() => undefined);
  });
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.queryByLabelText('Base URL')).not.toBeInTheDocument();
  expect(screen.getByText('https://old.example/v1', { exact: true })).toBeVisible();
});

it('an edit answered after the connection was deleted cannot resurrect it', async () => {
  const save = deferred<api.ModelConnection>();
  vi.mocked(api.updateModelConnection).mockReturnValue(save.promise);
  vi.mocked(api.deleteModelConnection).mockResolvedValue();
  mount();
  await screen.findByRole('article', { name: 'My provider' });
  editBaseUrl('https://new.example/v1');
  fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() =>
    expect(screen.queryByRole('article', { name: 'My provider' })).not.toBeInTheDocument(),
  );
  vi.mocked(api.listModelConnections).mockResolvedValue({
    configured: true,
    enabled: true,
    connections: [],
  });
  await act(async () => {
    save.resolve({ ...connection, base_url: 'https://new.example/v1', updated_at: 'later' });
    await save.promise;
  });
  await waitFor(() => expect(api.listModelConnections).toHaveBeenCalledTimes(2));
  expect(screen.queryByRole('article', { name: 'My provider' })).not.toBeInTheDocument();
});

it('leaving the page while verifying applies nothing and sends nothing more', async () => {
  const save = deferred<api.ModelConnection>();
  vi.mocked(api.updateModelConnection).mockReturnValue(save.promise);
  const page = mount();
  await screen.findByRole('article', { name: 'My provider' });
  editBaseUrl('https://new.example/v1');
  page.unmount();
  await act(async () => {
    save.resolve({ ...connection, base_url: 'https://new.example/v1', updated_at: 'later' });
    await save.promise;
  });
  expect(api.updateModelConnection).toHaveBeenCalledTimes(1);
  expect(api.createModelConnection).not.toHaveBeenCalled();
});

it('a refused edit keeps the old card, and any change clears the stale per-model results', async () => {
  vi.mocked(api.updateModelConnection).mockRejectedValue(
    new APIError(api.VERIFICATION_FAILED, 'no', 422, {
      models: [{ ...ready('m1'), status: 'failed', runtimes: [], message: 'Key refused.' }],
    }),
  );
  mount();
  await screen.findByRole('article', { name: 'My provider' });
  editBaseUrl('https://new.example/v1');
  expect(await screen.findByText('Not usable: Key refused.')).toBeVisible();
  expect(screen.getByText('https://old.example/v1', { exact: true })).toBeVisible();
  // The results described the endpoint as it was typed; an edit makes them stale.
  fireEvent.change(screen.getByLabelText('Base URL'), {
    target: { value: 'https://fixed.example/v1' },
  });
  expect(screen.queryByText('Not usable: Key refused.')).not.toBeInTheDocument();
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByLabelText('Base URL')).toHaveValue('https://fixed.example/v1');
});

it('a verifying save cannot be submitted twice', async () => {
  const save = deferred<api.ModelConnection>();
  vi.mocked(api.updateModelConnection).mockReturnValue(save.promise);
  mount();
  await screen.findByRole('article', { name: 'My provider' });
  editBaseUrl('https://new.example/v1');
  const busy = screen.getByRole('button', { name: 'Verifying…' });
  expect(busy).toBeDisabled();
  fireEvent.click(busy);
  expect(api.updateModelConnection).toHaveBeenCalledTimes(1);
  await act(async () => {
    save.resolve({ ...connection, base_url: 'https://new.example/v1', updated_at: 'later' });
    await save.promise;
  });
  expect(await screen.findByText('https://new.example/v1', { exact: true })).toBeVisible();
});
