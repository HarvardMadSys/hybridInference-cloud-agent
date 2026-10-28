// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import * as api from '@/lib/api/modelConnections';
import { ModelConnectionsSettings } from './ModelConnectionsSettings';

vi.mock('@/lib/api/modelConnections', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/modelConnections')>()),
  listModelConnections: vi.fn(),
  createModelConnection: vi.fn(),
  updateModelConnection: vi.fn(),
  deleteModelConnection: vi.fn(),
  verifyModelConnection: vi.fn(),
  discoverConnectionModels: vi.fn(),
}));

const pending = (model: string): api.ModelStatus => ({
  model,
  status: 'unverified',
  category: null,
  message: '',
  runtimes: [],
  paths: [],
  protocols: {},
  checked_at: null,
});
const connection: api.ModelConnection = {
  id: `mconn_${'e'.repeat(32)}`,
  name: 'My provider',
  base_url: 'https://old.example/v1',
  protocol_mode: 'auto',
  manual_protocols: [],
  models: ['m1', 'm2'],
  enabled: true,
  has_api_key: true,
  runtimes: [],
  model_status: [pending('m1'), pending('m2')],
  created_at: '2026-10-28T00:00:00Z',
  updated_at: '2026-10-28T00:00:00Z',
};
function deferredVerification() {
  let resolve!: (value: api.ModelVerification) => void;
  const promise = new Promise<api.ModelVerification>((done) => {
    resolve = done;
  });
  vi.mocked(api.verifyModelConnection).mockReturnValue(promise);
  return async () => {
    await act(async () => {
      resolve({ ...pending('m1'), status: 'ready', runtimes: ['codex'], recorded: true });
      await promise;
    });
  };
}
function mount() {
  return render(
    <LocaleProvider>
      <ModelConnectionsSettings />
    </LocaleProvider>,
  );
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

it('a late recorded result cannot restore the endpoint that was edited while it was in flight', async () => {
  const settle = deferredVerification();
  vi.mocked(api.updateModelConnection).mockResolvedValue({
    ...connection,
    base_url: 'https://new.example/v1',
    updated_at: '2026-10-28T01:00:00Z',
  });
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Verify m1' }));
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  fireEvent.change(screen.getByLabelText('Base URL'), {
    target: { value: 'https://new.example/v1' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await screen.findByText('https://new.example/v1', { exact: true });
  await settle();
  expect(screen.getByText('https://new.example/v1', { exact: true })).toBeVisible();
  expect(screen.queryByText('Ready', { exact: true })).not.toBeInTheDocument();
});

it('a verification completing while an edit is open keeps the editor and its unsaved input', async () => {
  const settle = deferredVerification();
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Verify m1' }));
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  fireEvent.change(screen.getByLabelText('Connection name'), { target: { value: 'Unsaved name' } });
  await settle();
  expect(screen.getByLabelText('Connection name')).toHaveValue('Unsaved name');
});

it('a late recorded result cannot resurrect a deleted connection', async () => {
  const settle = deferredVerification();
  vi.mocked(api.deleteModelConnection).mockResolvedValue();
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Verify m1' }));
  fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
  fireEvent.click(screen.getByRole('button', { name: 'Confirm' }));
  await waitFor(() =>
    expect(screen.queryByRole('article', { name: 'My provider' })).not.toBeInTheDocument(),
  );
  await settle();
  expect(screen.queryByRole('article', { name: 'My provider' })).not.toBeInTheDocument();
});

it('leaving the settings page stops the remaining verification batch requests', async () => {
  const settle = deferredVerification();
  const page = mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Verify unverified models (2)' }));
  expect(api.verifyModelConnection).toHaveBeenCalledTimes(1);
  page.unmount();
  await settle();
  expect(api.verifyModelConnection).toHaveBeenCalledTimes(1);
});

it('a recorded result queued with a newer edit cannot mark the changed endpoint ready', async () => {
  let finishVerification!: (value: api.ModelVerification) => void;
  let finishEdit!: (value: api.ModelConnection) => void;
  const verification = new Promise<api.ModelVerification>((done) => {
    finishVerification = done;
  });
  const edit = new Promise<api.ModelConnection>((done) => {
    finishEdit = done;
  });
  vi.mocked(api.verifyModelConnection).mockReturnValue(verification);
  vi.mocked(api.updateModelConnection).mockReturnValue(edit);
  mount();
  fireEvent.click(await screen.findByRole('button', { name: 'Verify m1' }));
  fireEvent.click(screen.getByRole('button', { name: 'Edit' }));
  fireEvent.change(screen.getByLabelText('Base URL'), {
    target: { value: 'https://new.example/v1' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'Save' }));
  await act(async () => {
    finishEdit({
      ...connection,
      base_url: 'https://new.example/v1',
      updated_at: '2026-10-28T01:00:00Z',
    });
    await edit;
    finishVerification({ ...pending('m1'), status: 'ready', runtimes: ['codex'], recorded: true });
    await verification;
  });
  expect(screen.getByText('https://new.example/v1', { exact: true })).toBeVisible();
  expect(screen.queryByText('Ready', { exact: true })).not.toBeInTheDocument();
});
