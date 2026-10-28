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
  discoverModels: vi.fn(),
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

it('cannot cancel a pending create that can still commit on the server', async () => {
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
  const cancel = screen.getByRole('button', { name: 'Cancel' });
  expect(cancel).toBeDisabled();
  fireEvent.click(cancel);
  expect(screen.getByLabelText('API key')).toHaveValue('fixture-key');
  expect(screen.queryByRole('article', { name: 'Late' })).not.toBeInTheDocument();
  await act(async () => {
    save.resolve({ ...connection, id: `mconn_${'f'.repeat(32)}`, name: 'Late' });
    await save.promise;
  });
  expect(screen.getByRole('article', { name: 'Late' })).toBeVisible();
  expect(screen.queryByLabelText('API key')).not.toBeInTheDocument();
});

it('can cancel after a refused save, leaving the saved configuration unchanged', async () => {
  const save = deferred<api.ModelConnection>();
  vi.mocked(api.updateModelConnection).mockReturnValue(save.promise);
  mount();
  await screen.findByRole('article', { name: 'My provider' });
  editBaseUrl('https://new.example/v1');
  expect(screen.getByRole('button', { name: 'Cancel' })).toBeDisabled();
  await act(async () => {
    save.reject(new APIError(api.VERIFICATION_FAILED, 'no', 422, { models: [] }));
    await save.promise.catch(() => undefined);
  });
  expect(screen.getByRole('alert')).toBeVisible();
  expect(screen.getByLabelText('Base URL')).toHaveValue('https://new.example/v1');
  fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
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
      models: [
        {
          ...ready('m1'),
          status: 'failed',
          category: 'auth',
          runtimes: [],
          message: 'Key refused.',
        },
      ],
    }),
  );
  mount();
  await screen.findByRole('article', { name: 'My provider' });
  editBaseUrl('https://new.example/v1');
  expect(
    await screen.findByText('Not usable: The provider refused the API key (401/403).'),
  ).toBeVisible();
  expect(screen.getByText('https://old.example/v1', { exact: true })).toBeVisible();
  // The results described the endpoint as it was typed; an edit makes them stale.
  fireEvent.change(screen.getByLabelText('Base URL'), {
    target: { value: 'https://fixed.example/v1' },
  });
  expect(
    screen.queryByText('Not usable: The provider refused the API key (401/403).'),
  ).not.toBeInTheDocument();
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

it.each(['edit-first', 'disable-first', 'same-batch'])(
  'overlapping edit and disable re-read the committed state (%s)',
  async (order) => {
    const edit = deferred<api.ModelConnection>();
    const disable = deferred<api.ModelConnection>();
    const edited = { ...connection, base_url: 'https://new.example/v1', updated_at: 'edited' };
    const disabled = { ...edited, enabled: false, updated_at: 'disabled' };
    vi.mocked(api.updateModelConnection).mockImplementation((_id, changes) =>
      changes.enabled === false ? disable.promise : edit.promise,
    );
    mount();
    await screen.findByRole('article', { name: 'My provider' });
    editBaseUrl(edited.base_url);
    fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
    fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
    vi.mocked(api.listModelConnections).mockResolvedValue({
      configured: true,
      enabled: true,
      connections: [disabled],
    });
    if (order === 'same-batch') {
      await act(async () => {
        edit.resolve(edited);
        disable.resolve(disabled);
        await Promise.all([edit.promise, disable.promise]);
      });
    } else {
      const first = order === 'edit-first' ? edit : disable;
      const last = order === 'edit-first' ? disable : edit;
      await act(async () => {
        first.resolve(order === 'edit-first' ? edited : disabled);
        await first.promise;
      });
      await act(async () => {
        last.resolve(order === 'edit-first' ? disabled : edited);
        await last.promise;
      });
    }
    await waitFor(() => expect(api.listModelConnections).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole('button', { name: 'Enable' })).toBeVisible();
    expect(screen.queryByRole('button', { name: 'Disable' })).not.toBeInTheDocument();
    expect(screen.getByText(edited.base_url, { exact: true })).toBeVisible();
  },
);

it('a delayed list refresh cannot restore a connection deleted by a later action', async () => {
  const edit = deferred<api.ModelConnection>();
  const disable = deferred<api.ModelConnection>();
  const refresh = deferred<api.ModelConnectionList>();
  vi.mocked(api.updateModelConnection).mockImplementation((_id, changes) =>
    changes.enabled === false ? disable.promise : edit.promise,
  );
  vi.mocked(api.deleteModelConnection).mockResolvedValue();
  mount();
  await screen.findByRole('article', { name: 'My provider' });
  editBaseUrl('https://new.example/v1');
  fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  vi.mocked(api.listModelConnections)
    .mockReturnValueOnce(refresh.promise)
    .mockResolvedValue({ configured: true, enabled: true, connections: [] });
  await act(async () => {
    edit.resolve(connection);
    disable.resolve({ ...connection, enabled: false });
    await Promise.all([edit.promise, disable.promise]);
  });
  await waitFor(() => expect(api.listModelConnections).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() =>
    expect(screen.queryByRole('article', { name: 'My provider' })).not.toBeInTheDocument(),
  );
  await act(async () => {
    refresh.resolve({ configured: true, enabled: true, connections: [connection] });
    await refresh.promise;
  });
  await waitFor(() => expect(api.listModelConnections).toHaveBeenCalledTimes(3));
  expect(screen.queryByRole('article', { name: 'My provider' })).not.toBeInTheDocument();
});

it('saving an unchanged editor does not restore the snapshot from before a disable', async () => {
  vi.mocked(api.updateModelConnection).mockResolvedValue({
    ...connection,
    enabled: false,
    updated_at: 'disabled',
  });
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Edit' }));
  fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await screen.findByRole('button', { name: 'Enable' });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await waitFor(() => expect(screen.queryByLabelText('API key')).not.toBeInTheDocument());
  expect(screen.getByRole('button', { name: 'Enable' })).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Disable' })).not.toBeInTheDocument();
  expect(api.updateModelConnection).toHaveBeenCalledTimes(1);
});

it('a delayed list refresh cannot discard a connection created after the read began', async () => {
  const edit = deferred<api.ModelConnection>();
  const disable = deferred<api.ModelConnection>();
  const refresh = deferred<api.ModelConnectionList>();
  const added = { ...connection, id: `mconn_${'f'.repeat(32)}`, name: 'New provider' };
  vi.mocked(api.updateModelConnection).mockImplementation((_id, changes) =>
    changes.enabled === false ? disable.promise : edit.promise,
  );
  vi.mocked(api.createModelConnection).mockResolvedValue(added);
  mount();
  await screen.findByRole('article', { name: 'My provider' });
  editBaseUrl('https://new.example/v1');
  fireEvent.click(screen.getByRole('button', { name: 'Disable' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  vi.mocked(api.listModelConnections)
    .mockReturnValueOnce(refresh.promise)
    .mockResolvedValue({ configured: true, enabled: true, connections: [connection, added] });
  await act(async () => {
    edit.resolve(connection);
    disable.resolve({ ...connection, enabled: false });
    await Promise.all([edit.promise, disable.promise]);
  });
  await waitFor(() => expect(api.listModelConnections).toHaveBeenCalledTimes(2));
  fireEvent.click(screen.getByRole('button', { name: 'Add connection' }));
  fireEvent.change(screen.getByLabelText('Connection name'), { target: { value: added.name } });
  fireEvent.change(screen.getByLabelText('Base URL'), { target: { value: connection.base_url } });
  fireEvent.change(screen.getByLabelText('API key'), { target: { value: 'fixture-key' } });
  fireEvent.change(screen.getByLabelText('Model IDs'), { target: { value: 'm1' } });
  fireEvent.click(screen.getByRole('button', { name: 'Verify and save' }));
  await screen.findByRole('article', { name: added.name });
  await act(async () => {
    refresh.resolve({ configured: true, enabled: true, connections: [connection] });
    await refresh.promise;
  });
  await waitFor(() => expect(api.listModelConnections).toHaveBeenCalledTimes(3));
  expect(screen.getByRole('article', { name: added.name })).toBeVisible();
});
