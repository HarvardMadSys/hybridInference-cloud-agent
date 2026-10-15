// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { EnvironmentDialog } from './EnvironmentDialog';
import {
  importEnvironmentVariables,
  previewEnvironmentImport,
  saveEnvironment,
  saveGlobalVariables,
  suggestEnvironmentScript,
  testEnvironment,
} from '@/lib/api/environments';
import type { EnvironmentConfig } from '@/lib/api/environments';
vi.mock('@/components/providers', () => ({ useT: () => (key: string) => key }));
vi.mock('@/lib/api/environments', () => ({
  cancelEnvironmentRun: vi.fn(),
  getEnvironmentHistory: vi.fn(),
  getEnvironmentRun: vi.fn(),
  importEnvironmentVariables: vi.fn(),
  previewEnvironmentImport: vi.fn(),
  restoreEnvironment: vi.fn(),
  saveEnvironment: vi.fn(),
  saveGlobalVariables: vi.fn(),
  suggestEnvironmentScript: vi.fn(),
  testEnvironment: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const config: EnvironmentConfig = {
  id: 'env',
  repo: 'org/repo',
  name: 'Dev',
  revision: 1,
  variables: [],
  setup_script: '',
  start_script: '',
  verify_script: '',
  actions: [],
};
const globals: EnvironmentConfig = { ...config, id: null, repo: null, name: 'Global', revision: 0 };

function open(initial = config, onClose = vi.fn()) {
  render(
    <EnvironmentDialog initial={initial} globals={globals} onSaved={vi.fn()} onClose={onClose} />,
  );
  return onClose;
}

it('requires explicit Plain classification before importing a short value', async () => {
  vi.mocked(previewEnvironmentImport).mockResolvedValue({
    variables: [{ name: 'DEBUG', operation: 'add', secret: true, secret_allowed: false }],
  });
  vi.mocked(importEnvironmentVariables).mockResolvedValue({ ...config, revision: 2 });
  open();
  fireEvent.click(screen.getByRole('button', { name: 'environment.importEnv' }));
  fireEvent.change(screen.getByLabelText('environment.dotenv'), { target: { value: 'DEBUG=1' } });
  fireEvent.click(screen.getByRole('button', { name: 'environment.previewImport' }));
  const confirm = await screen.findByRole('button', { name: 'environment.confirmImport' });
  expect(confirm).toBeDisabled();
  expect(screen.getByRole('alert')).toHaveTextContent('environment.shortSecret');
  fireEvent.change(screen.getByLabelText('environment.type DEBUG'), {
    target: { value: 'ordinary' },
  });
  expect(confirm).toBeEnabled();
  fireEvent.click(confirm);
  // Nothing unsaved, so the import applies to the saved version as it is.
  await waitFor(() =>
    expect(importEnvironmentVariables).toHaveBeenCalledWith('env', 1, 0, 'DEBUG=1', ['DEBUG']),
  );
  expect(saveEnvironment).not.toHaveBeenCalled();
});

it('saves a new environment before previewing an import into it', async () => {
  vi.mocked(saveEnvironment).mockResolvedValue({ ...config, id: 'created' });
  vi.mocked(previewEnvironmentImport).mockResolvedValue({ variables: [] });
  open({ ...config, id: null, name: 'Fresh', revision: 0 });
  fireEvent.click(screen.getByRole('button', { name: 'environment.importEnv' }));
  expect(screen.getByText('environment.importSavesFirst')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('environment.dotenv'), { target: { value: 'A=1' } });
  fireEvent.click(screen.getByRole('button', { name: 'environment.previewImport' }));
  await waitFor(() => expect(previewEnvironmentImport).toHaveBeenCalledWith('created', 1, 'A=1'));
  expect(saveEnvironment).toHaveBeenCalledOnce();
});

it('asks before discarding unsaved changes', async () => {
  const onClose = open();
  fireEvent.change(screen.getByPlaceholderText('environment.namePlaceholder'), {
    target: { value: 'Renamed' },
  });
  expect(screen.getByRole('status')).toHaveTextContent('environment.unsaved');
  fireEvent.click(screen.getAllByRole('button', { name: 'environment.close' }).at(-1)!);
  expect(onClose).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: 'environment.keepEditing' }));
  expect(screen.getByPlaceholderText('environment.namePlaceholder')).toHaveValue('Renamed');
  fireEvent.keyDown(window, { key: 'Escape' });
  fireEvent.click(screen.getByRole('button', { name: 'environment.discard' }));
  expect(onClose).toHaveBeenCalledOnce();
});

it('closes at once when nothing has changed', () => {
  const onClose = open();
  fireEvent.keyDown(window, { key: 'Escape' });
  expect(onClose).toHaveBeenCalledOnce();
});

it('names what a new environment is missing instead of saving it', async () => {
  open({ ...config, id: null, repo: '', name: '', revision: 0 });
  fireEvent.click(screen.getByRole('button', { name: 'environment.save' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('environment.nameRequired');
  fireEvent.change(screen.getByPlaceholderText('environment.namePlaceholder'), {
    target: { value: 'Dev' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'environment.save' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('environment.repositoryRequired');
  expect(saveEnvironment).not.toHaveBeenCalled();
});

it('saves pending edits, then tests that version and shows its progress', async () => {
  vi.mocked(saveEnvironment).mockResolvedValue({ ...config, revision: 2, setup_script: 'npm ci' });
  vi.mocked(testEnvironment).mockResolvedValue({
    id: 'run',
    state: 'running',
    steps: [{ phase: 'setup', state: 'running', output: 'installing' }],
    base_sha: 'abcdef0123456789',
  });
  open();
  fireEvent.click(screen.getByRole('tab', { name: 'environment.tab.scripts' }));
  fireEvent.change(screen.getByLabelText('environment.installScript'), {
    target: { value: 'npm ci' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'environment.saveAndTest' }));
  await waitFor(() => expect(testEnvironment).toHaveBeenCalledWith('env', 2, 0));
  expect(vi.mocked(saveEnvironment).mock.calls[0][0]).toMatchObject({ setup_script: 'npm ci' });
  expect(screen.getByRole('tab', { name: 'environment.tab.test' })).toHaveAttribute(
    'aria-selected',
    'true',
  );
  expect(screen.getAllByText('environment.state.running').length).toBeGreaterThan(0);
  expect(screen.getByText('environment.phase.setup')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'environment.cancelTest' })).toBeInTheDocument();
});

it('offers each lockfile suggestion to choose instead of reporting them as an error', async () => {
  vi.mocked(suggestEnvironmentScript).mockResolvedValue({
    suggestions: [
      { file: 'package-lock.json', script: 'npm ci' },
      { file: 'uv.lock', script: 'uv sync --frozen' },
    ],
  });
  open();
  fireEvent.click(screen.getByRole('tab', { name: 'environment.tab.scripts' }));
  fireEvent.click(screen.getByRole('button', { name: 'environment.suggestScript' }));
  const choice = (await screen.findByText('uv sync --frozen')).closest('li')!;
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  fireEvent.click(within(choice).getByRole('button', { name: 'environment.useSuggestion' }));
  expect(screen.getByLabelText('environment.installScript')).toHaveValue('uv sync --frozen');
});

it('edits global variables without repository sections', async () => {
  vi.mocked(saveGlobalVariables).mockResolvedValue({ ...globals, revision: 1 });
  open(globals);
  expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'environment.saveAndTest' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /environment.addVariable/ }));
  fireEvent.change(screen.getByLabelText('environment.variableName'), {
    target: { value: 'API_URL' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'environment.save' }));
  await waitFor(() =>
    expect(saveGlobalVariables).toHaveBeenCalledWith(0, [
      expect.objectContaining({ name: 'API_URL', operation: 'replace' }),
    ]),
  );
  expect(await screen.findByRole('status')).toHaveTextContent('environment.savedGlobal');
});

it('saves an update script for workspaces that start from a snapshot', async () => {
  vi.mocked(saveEnvironment).mockResolvedValue({ ...config, revision: 2 });
  open();
  fireEvent.click(screen.getByRole('tab', { name: 'environment.tab.scripts' }));
  fireEvent.change(screen.getByLabelText('environment.updateScript'), {
    target: { value: 'npm run generate' },
  });
  fireEvent.click(screen.getByRole('button', { name: 'environment.save' }));
  await waitFor(() =>
    expect(vi.mocked(saveEnvironment).mock.calls[0][0]).toMatchObject({
      update_script: 'npm run generate',
    }),
  );
});
