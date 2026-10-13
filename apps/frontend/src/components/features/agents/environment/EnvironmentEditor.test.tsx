// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { EnvironmentEditor } from './EnvironmentEditor';
import {
  importEnvironmentVariables,
  previewEnvironmentImport,
  saveEnvironment,
} from '@/lib/api/environments';
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
afterEach(cleanup);
it('requires explicit Ordinary classification before importing a short value', async () => {
  const config = {
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
  vi.mocked(previewEnvironmentImport).mockResolvedValue({
    variables: [{ name: 'DEBUG', operation: 'add', secret: true, secret_allowed: false }],
  });
  vi.mocked(saveEnvironment).mockResolvedValue({ ...config, revision: 2 });
  vi.mocked(importEnvironmentVariables).mockResolvedValue({ ...config, revision: 3 });
  render(
    <EnvironmentEditor
      initial={config}
      globals={{ ...config, id: null, repo: null, revision: 0 }}
      onSaved={vi.fn()}
    />,
  );
  fireEvent.click(screen.getByText('environment.importEnv'));
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
  await waitFor(() =>
    expect(importEnvironmentVariables).toHaveBeenCalledWith('env', 2, 0, 'DEBUG=1', ['DEBUG']),
  );
  await waitFor(() => expect(screen.getByLabelText('environment.dotenv')).toHaveValue(''));
});
