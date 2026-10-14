// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import {
  getWorkspaceEnvironment,
  refreshWorkspaceRuntime,
  skipWorkspaceStart,
} from '@/lib/api/environments';
import { WorkspaceEnvironment } from './WorkspaceEnvironment';
vi.mock('@/components/providers', () => ({ useT: () => (key: string) => key }));
vi.mock('./EnvironmentManager', () => ({ EnvironmentManager: () => null }));
vi.mock('./EnvironmentEditor', () => ({ EnvironmentSteps: () => null }));
vi.mock('@/lib/api/environments', () => ({
  getWorkspaceEnvironment: vi.fn(),
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
it('updates runtime by version and explicitly arms only one startup recovery', async () => {
  vi.mocked(getWorkspaceEnvironment).mockResolvedValue({
    binding_id: 'b',
    legacy: false,
    runtime_revision: 3,
    variables: [],
    setup_script: 'original',
    start_script: 'start',
    actions: [],
    steps: [{ phase: 'start', state: 'failed', output: '' }],
  });
  vi.mocked(refreshWorkspaceRuntime).mockResolvedValue({});
  vi.mocked(skipWorkspaceStart).mockResolvedValue({});
  render(<WorkspaceEnvironment jobId="job" repo="org/repo" onTerminal={() => {}} />);
  fireEvent.click(await screen.findByRole('button', { name: 'environment.refreshRuntime' }));
  await waitFor(() => expect(refreshWorkspaceRuntime).toHaveBeenCalledWith('job', 3));
  fireEvent.click(screen.getByRole('button', { name: 'environment.skipStart' }));
  await waitFor(() => expect(skipWorkspaceStart).toHaveBeenCalledWith('job'));
  expect(await screen.findByText('environment.skipArmed')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'environment.skipStart' })).toBeDisabled();
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
