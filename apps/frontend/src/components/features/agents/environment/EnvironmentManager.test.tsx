// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getAgentConfig } from '@/lib/api/agents';
import {
  copyEnvironment,
  deleteEnvironment,
  listEnvironments,
  setEnvironmentDefault,
} from '@/lib/api/environments';
import type { EnvironmentConfig } from '@/lib/api/environments';
import { EnvironmentManager } from './EnvironmentManager';
vi.mock('@/components/providers', () => ({ useT: () => (key: string) => key }));
vi.mock('@/lib/api/agents', () => ({ getAgentConfig: vi.fn() }));
vi.mock('@/lib/api/environments', () => ({
  copyEnvironment: vi.fn(),
  deleteEnvironment: vi.fn(),
  listEnvironments: vi.fn(),
  setEnvironmentDefault: vi.fn(),
}));
vi.mock('./EnvironmentDialog', () => ({
  EnvironmentDialog: ({ initial }: { initial: { name: string; repo: string | null } }) => (
    <div role="dialog" aria-label={`editing ${initial.repo === null ? 'globals' : initial.name}`} />
  ),
}));

function environment(fields: Partial<EnvironmentConfig>): EnvironmentConfig {
  return {
    id: 'env',
    repo: 'org/web',
    name: 'Development',
    revision: 3,
    variables: [],
    setup_script: '',
    start_script: '',
    verify_script: '',
    actions: [],
    ...fields,
  };
}

beforeEach(() => {
  vi.mocked(getAgentConfig).mockResolvedValue({ repos: ['org/api', 'org/web'] } as never);
  vi.mocked(listEnvironments).mockResolvedValue({
    globals: environment({ id: 'global', repo: null, name: 'Global', revision: 2 }),
    environments: [
      environment({
        is_default: true,
        latest_test: { id: 'run', state: 'succeeded', revision: 3, global_revision: 2 },
      }),
      environment({ id: 'ci', name: 'Integration', revision: 1 }),
      environment({ id: 'api', repo: 'org/api', name: 'Base' }),
    ],
  });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it('groups environments by repository with their default and test state', async () => {
  render(<EnvironmentManager />);
  const web = (await screen.findByText('org/web')).parentElement!;
  expect(within(web).getByText('Development')).toBeInTheDocument();
  expect(within(web).getByText('environment.default')).toBeInTheDocument();
  expect(within(web).getByText('environment.test.passed')).toBeInTheDocument();
  expect(within(web).getByText('environment.test.never')).toBeInTheDocument();
  expect(screen.getByText('org/api').parentElement).toHaveTextContent('Base');
  expect(screen.getByText('environment.globalVariables')).toBeInTheDocument();
});

it('opens the shared editor for global variables, an environment, or a new one', async () => {
  render(<EnvironmentManager />);
  await screen.findByText('org/web');
  fireEvent.click(screen.getAllByRole('button', { name: 'environment.edit' })[0]);
  expect(screen.getByRole('dialog', { name: 'editing globals' })).toBeInTheDocument();
});

it('asks before removing an environment', async () => {
  vi.mocked(deleteEnvironment).mockResolvedValue(undefined);
  render(<EnvironmentManager />);
  await screen.findByText('org/web');
  fireEvent.click(screen.getAllByRole('button', { name: 'environment.manageItem' })[2]);
  fireEvent.click(screen.getByRole('menuitem', { name: 'environment.remove' }));
  expect(deleteEnvironment).not.toHaveBeenCalled();
  expect(screen.getByText('environment.removeConfirm')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'environment.remove' }));
  await waitFor(() => expect(deleteEnvironment).toHaveBeenCalledWith('ci', 1));
});

it('offers Make default only for an environment that is not already the default', async () => {
  vi.mocked(setEnvironmentDefault).mockResolvedValue({});
  render(<EnvironmentManager />);
  await screen.findByText('org/web');
  fireEvent.click(screen.getAllByRole('button', { name: 'environment.manageItem' })[1]);
  expect(screen.queryByRole('menuitem', { name: 'environment.makeDefault' })).toBeNull();
  fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
  fireEvent.click(screen.getAllByRole('button', { name: 'environment.manageItem' })[2]);
  fireEvent.click(screen.getByRole('menuitem', { name: 'environment.makeDefault' }));
  await waitFor(() => expect(setEnvironmentDefault).toHaveBeenCalledWith('org/web', 'ci', 0));
});

it('copies with one idempotency key per request and opens the copy', async () => {
  vi.mocked(copyEnvironment)
    .mockRejectedValueOnce(new Error('Network error'))
    .mockResolvedValueOnce(environment({ id: 'copy', name: 'Copied' }));
  render(<EnvironmentManager />);
  await screen.findByText('org/web');
  fireEvent.click(screen.getAllByRole('button', { name: 'environment.manageItem' })[1]);
  fireEvent.click(screen.getByRole('menuitem', { name: 'environment.copy' }));
  fireEvent.click(screen.getByRole('button', { name: 'environment.copy' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Network error');
  fireEvent.click(screen.getByRole('button', { name: 'environment.copy' }));
  expect(await screen.findByRole('dialog', { name: 'editing Copied' })).toBeInTheDocument();
  const [first, second] = vi.mocked(copyEnvironment).mock.calls;
  expect(first[4]).toBe(second[4]);
  expect(first.slice(0, 4)).toEqual(['env', 3, 'org/web', 'environment.copyName']);
});
