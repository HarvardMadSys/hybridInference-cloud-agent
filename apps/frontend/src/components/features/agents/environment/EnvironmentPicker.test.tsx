// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { listEnvironments } from '@/lib/api/environments';
import { EnvironmentPicker } from './EnvironmentPicker';
import { VariableEditor, variableEdits } from './VariableEditor';
vi.mock('@/components/providers', () => ({ useT: () => (key: string) => key }));
vi.mock('@/lib/api/environments', () => ({ listEnvironments: vi.fn() }));
vi.mock('./EnvironmentDialog', () => ({
  EnvironmentDialog: ({ initial, onClose }: { initial: { name: string }; onClose: () => void }) => (
    <div role="dialog" aria-label={`editing ${initial.name}`}>
      <button type="button" onClick={onClose}>
        close editor
      </button>
    </div>
  ),
}));
const globals = {
  id: 'global',
  repo: null,
  name: 'Global',
  revision: 2,
  variables: [],
  setup_script: '',
  start_script: '',
  verify_script: '',
  actions: [],
};
beforeEach(() => {
  vi.mocked(listEnvironments).mockResolvedValue({
    globals,
    environments: [
      {
        ...globals,
        id: 'env',
        repo: 'org/repo',
        name: 'Development',
        revision: 4,
        is_default: true,
      },
    ],
  });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});
it('pins default and global versions and allows explicit platform-only selection', async () => {
  const change = vi.fn();
  render(<EnvironmentPicker repo="org/repo" open onClose={vi.fn()} onChange={change} />);
  await waitFor(() =>
    expect(change).toHaveBeenLastCalledWith({
      environment_id: 'env',
      environment_revision: 4,
      global_revision: 2,
    }),
  );
  expect(screen.getByRole('radio', { name: /Development/ })).toBeChecked();
  fireEvent.click(screen.getByRole('radio', { name: /environment.none/ }));
  await waitFor(() =>
    expect(change).toHaveBeenLastCalledWith({
      environment_id: null,
      environment_revision: 0,
      global_revision: 2,
    }),
  );
});
it('edits the selected environment or a new one in the shared dialog', async () => {
  render(<EnvironmentPicker repo="org/repo" open onClose={vi.fn()} onChange={vi.fn()} />);
  await screen.findByRole('radio', { name: /Development/ });
  fireEvent.click(screen.getByRole('button', { name: 'environment.editItem' }));
  expect(screen.getByRole('dialog', { name: 'editing Development' })).toBeInTheDocument();
});
it('reads the environments again when the editor closes, so a test run there shows', async () => {
  const development = { ...globals, id: 'env', repo: 'org/repo', name: 'Development' };
  vi.mocked(listEnvironments)
    .mockResolvedValue({
      globals,
      environments: [
        {
          ...development,
          latest_test: { id: 'run', state: 'succeeded', revision: 2, global_revision: 2 },
        },
      ],
    })
    .mockResolvedValueOnce({ globals, environments: [development] });
  render(<EnvironmentPicker repo="org/repo" open onClose={vi.fn()} onChange={vi.fn()} />);
  const option = await screen.findByRole('radio', { name: /Development/ });
  expect(option.closest('label')).toHaveTextContent('environment.test.never');
  fireEvent.click(option);
  fireEvent.click(screen.getByRole('button', { name: 'environment.editItem' }));
  fireEvent.click(screen.getByRole('button', { name: 'close editor' }));
  await waitFor(() =>
    expect(screen.getByRole('radio', { name: /Development/ }).closest('label')).toHaveTextContent(
      'environment.test.passed',
    ),
  );
});
it('keeps a saved Secret blank and requires an explicit replacement when changing its type', () => {
  const change = vi.fn();
  render(
    <VariableEditor
      values={variableEdits([{ name: 'TOKEN', secret: true, phase: 'both', value: null }])}
      onChange={change}
    />,
  );
  expect(screen.getByLabelText('environment.value TOKEN')).toHaveValue('');
  // A saved name is shown, not offered for editing.
  expect(screen.queryByLabelText('environment.variableName')).toBeNull();
  expect(screen.getByText('TOKEN')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('environment.type TOKEN'), {
    target: { value: 'ordinary' },
  });
  expect(change).toHaveBeenCalledWith([
    {
      name: 'TOKEN',
      original_name: 'TOKEN',
      original_secret: true,
      secret: false,
      phase: 'both',
      operation: 'replace',
      value: '',
    },
  ]);
});
it('does not admit a task while the environment selection cannot be loaded', async () => {
  let rejectLoad: (error: Error) => void = () => {};
  vi.mocked(listEnvironments).mockImplementation(
    () =>
      new Promise((_resolve, reject) => {
        rejectLoad = reject;
      }),
  );
  const change = vi.fn();
  render(<EnvironmentPicker repo="org/repo" open={false} onClose={vi.fn()} onChange={change} />);
  await act(async () => rejectLoad(new Error('Load failed')));
  expect(await screen.findByRole('alert')).toHaveTextContent('Load failed');
  expect(change).toHaveBeenCalledWith(null);
  expect(change).not.toHaveBeenCalledWith(expect.objectContaining({ environment_id: 'env' }));
});

it('leaves loading on failure and recovers through retry without silently dropping defaults', async () => {
  const status = vi.fn();
  const change = vi.fn();
  vi.mocked(listEnvironments).mockRejectedValueOnce(new Error('Temporary outage'));
  render(
    <EnvironmentPicker
      repo="org/repo"
      open={false}
      onClose={vi.fn()}
      onChange={change}
      onStatusChange={status}
    />,
  );
  expect(await screen.findByRole('alert')).toHaveTextContent('Temporary outage');
  expect(status).toHaveBeenLastCalledWith('error');
  expect(change).toHaveBeenLastCalledWith(null);
  fireEvent.click(screen.getByRole('button', { name: 'environment.retry' }));
  await waitFor(() => expect(status).toHaveBeenLastCalledWith('ready'));
  await waitFor(() =>
    expect(change).toHaveBeenLastCalledWith(
      expect.objectContaining({ environment_id: 'env', global_revision: 2 }),
    ),
  );
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});
it("marks a team's environment, and runs your install-only globals without it", async () => {
  vi.mocked(listEnvironments).mockResolvedValue({
    globals: {
      ...globals,
      variables: [
        { name: 'INSTALL_TOKEN', secret: true, phase: 'setup', value: null },
        { name: 'MY_KEY', secret: true, phase: 'both', value: null },
      ],
    },
    environments: [
      {
        ...globals,
        id: 'shared',
        repo: 'org/repo',
        name: 'Shared',
        revision: 1,
        is_default: true,
        team: { id: 'team-1', name: 'Platform' },
        can_edit: false,
      },
    ],
  });
  render(<EnvironmentPicker repo="org/repo" open onClose={vi.fn()} onChange={vi.fn()} />);
  const option = await screen.findByRole('radio', { name: /Shared/ });
  expect(option.closest('label')).toHaveTextContent('environment.teamBadge');
  expect(screen.getByRole('button', { name: 'environment.viewItem' })).toBeInTheDocument();
  fireEvent.click(screen.getByText('environment.details'));
  expect(screen.getByText('MY_KEY')).toBeInTheDocument();
  expect(screen.queryByText('INSTALL_TOKEN')).toBeNull();
});
