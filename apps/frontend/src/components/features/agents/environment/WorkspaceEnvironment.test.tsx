// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import {
  getWorkspaceEnvironment,
  listEnvironments,
  refreshWorkspaceRuntime,
  retryWorkspaceEnvironment,
  skipWorkspaceStart,
} from '@/lib/api/environments';
import type { EnvironmentBinding } from '@/lib/api/environments';
import { WorkspaceEnvironment } from './WorkspaceEnvironment';
vi.mock('@/components/providers', () => ({ useT: () => (key: string) => key }));
vi.mock('./EnvironmentDialog', () => ({
  EnvironmentDialog: ({ initial }: { initial: { name: string } }) => (
    <div role="dialog" aria-label={`editing ${initial.name}`} />
  ),
}));
vi.mock('@/lib/api/environments', () => ({
  getWorkspaceEnvironment: vi.fn(),
  getGlobalEnvironment: vi.fn(),
  listEnvironments: vi.fn(),
  refreshWorkspaceRuntime: vi.fn(),
  skipWorkspaceStart: vi.fn(),
  getEnvironmentActionRun: vi.fn(),
  retryWorkspaceEnvironment: vi.fn(),
  startEnvironmentAction: vi.fn(),
  stopEnvironmentAction: vi.fn(),
}));
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

const binding: EnvironmentBinding = {
  binding_id: 'b',
  environment_id: 'env',
  environment_name: 'Development',
  revision: 2,
  legacy: false,
  required: true,
  runtime_revision: 3,
  variables: [],
  setup_script: 'original',
  start_script: 'start',
  actions: [],
  steps: [],
};

it('leads with a failed start and arms only one startup recovery', async () => {
  vi.mocked(getWorkspaceEnvironment).mockResolvedValue({
    ...binding,
    steps: [
      { phase: 'setup', state: 'succeeded', output: '', duration_seconds: 4 },
      { phase: 'start', state: 'failed', output: 'redis: not found', exit_code: 127 },
    ],
  });
  vi.mocked(skipWorkspaceStart).mockResolvedValue({});
  vi.mocked(retryWorkspaceEnvironment).mockResolvedValue({});
  render(<WorkspaceEnvironment jobId="job" repo="org/repo" onTerminal={() => {}} />);
  expect(await screen.findByText('environment.status.failed')).toBeInTheDocument();
  expect(screen.getByText('environment.failedStep')).toBeInTheDocument();
  expect(screen.getByText('redis: not found')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'environment.skipStart' }));
  await waitFor(() => expect(skipWorkspaceStart).toHaveBeenCalledWith('job'));
  expect(await screen.findByText('environment.skipArmed')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'environment.skipStart' })).toBeDisabled();
  fireEvent.click(screen.getByRole('button', { name: 'environment.retryPreparation' }));
  await waitFor(() => expect(retryWorkspaceEnvironment).toHaveBeenCalledWith('job'));
});

it('updates runtime variables by their version', async () => {
  vi.mocked(getWorkspaceEnvironment).mockResolvedValue({
    ...binding,
    steps: [{ phase: 'setup', state: 'succeeded', output: '' }],
  });
  vi.mocked(refreshWorkspaceRuntime).mockResolvedValue({});
  render(<WorkspaceEnvironment jobId="job" repo="org/repo" onTerminal={() => {}} />);
  expect(await screen.findByText('environment.status.ready')).toBeInTheDocument();
  fireEvent.click(screen.getByText('environment.configuration'));
  fireEvent.click(screen.getByRole('button', { name: 'environment.refreshRuntime' }));
  await waitFor(() => expect(refreshWorkspaceRuntime).toHaveBeenCalledWith('job', 3));
});

it('says when preparation has not started yet', async () => {
  vi.mocked(getWorkspaceEnvironment).mockResolvedValue(binding);
  render(<WorkspaceEnvironment jobId="job" repo="org/repo" onTerminal={() => {}} />);
  expect(await screen.findByText('environment.status.pending')).toBeInTheDocument();
  expect(screen.getByText('environment.pendingHint')).toBeInTheDocument();
});

it('opens the saved environment as it is now in the shared editor', async () => {
  vi.mocked(getWorkspaceEnvironment).mockResolvedValue(binding);
  vi.mocked(listEnvironments).mockResolvedValue({
    globals: {
      id: null,
      name: 'Global',
      repo: null,
      revision: 0,
      variables: [],
      setup_script: '',
      start_script: '',
      verify_script: '',
      actions: [],
    },
    environments: [
      {
        id: 'env',
        name: 'Development',
        repo: 'org/repo',
        revision: 5,
        variables: [],
        setup_script: '',
        start_script: '',
        verify_script: '',
        actions: [],
      },
    ],
  });
  render(<WorkspaceEnvironment jobId="job" repo="org/repo" onTerminal={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: 'environment.editSaved' }));
  expect(await screen.findByRole('dialog', { name: 'editing Development' })).toBeInTheDocument();
  expect(listEnvironments).toHaveBeenCalledWith('org/repo');
});

it('clears a failed read of the environment once a later read succeeds', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    vi.mocked(getWorkspaceEnvironment)
      .mockRejectedValueOnce(new Error('Temporarily unavailable'))
      .mockResolvedValue({
        binding_id: 'b',
        legacy: false,
        runtime_revision: 1,
        variables: [],
        setup_script: '',
        start_script: '',
        actions: [],
        steps: [],
      });
    render(<WorkspaceEnvironment jobId="job" repo="org/repo" onTerminal={() => {}} />);
    expect(await screen.findByRole('alert')).toHaveTextContent('Temporarily unavailable');
    await vi.advanceTimersByTimeAsync(5000);
    await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());
  } finally {
    vi.useRealTimers();
  }
});
