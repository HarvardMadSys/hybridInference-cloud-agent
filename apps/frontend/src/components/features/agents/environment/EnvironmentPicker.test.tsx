// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { listEnvironments } from '@/lib/api/environments';
import { EnvironmentPicker } from './EnvironmentPicker';
import { VariableEditor, variableEdits } from './VariableEditor';
vi.mock('@/components/providers', () => ({ useT: () => (key: string) => key }));
vi.mock('@/lib/api/environments', () => ({ listEnvironments: vi.fn() }));
vi.mock('./EnvironmentManager', () => ({ EnvironmentManager: () => null }));
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
  render(<EnvironmentPicker repo="org/repo" onChange={change} />);
  await waitFor(() =>
    expect(change).toHaveBeenLastCalledWith({
      environment_id: 'env',
      environment_revision: 4,
      global_revision: 2,
      environment_overrides: [],
    }),
  );
  fireEvent.change(screen.getByRole('combobox', { name: 'environment.choose' }), {
    target: { value: '' },
  });
  await waitFor(() =>
    expect(change).toHaveBeenLastCalledWith({
      environment_id: null,
      environment_revision: 0,
      global_revision: 2,
      environment_overrides: [],
    }),
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
  expect(screen.getByLabelText('environment.variableName')).toBeDisabled();
  fireEvent.change(screen.getByLabelText('environment.type TOKEN'), {
    target: { value: 'ordinary' },
  });
  expect(change).toHaveBeenCalledWith([
    {
      name: 'TOKEN',
      original_name: 'TOKEN',
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
  render(<EnvironmentPicker repo="org/repo" onChange={change} />);
  await act(async () => rejectLoad(new Error('Load failed')));
  expect(await screen.findByRole('alert')).toHaveTextContent('Load failed');
  expect(change).toHaveBeenCalledWith(null);
  expect(change).not.toHaveBeenCalledWith(expect.objectContaining({ environment_id: 'env' }));
});

it('leaves loading on failure and recovers through retry without silently dropping defaults', async () => {
  const status = vi.fn();
  const change = vi.fn();
  vi.mocked(listEnvironments).mockRejectedValueOnce(new Error('Temporary outage'));
  render(<EnvironmentPicker repo="org/repo" onChange={change} onStatusChange={status} />);
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

it('ignores a blank newly added override row in the submitted selection', async () => {
  const change = vi.fn();
  render(<EnvironmentPicker repo="org/repo" onChange={change} />);
  await screen.findByRole('combobox', { name: 'environment.choose' });
  fireEvent.click(screen.getByRole('button', { name: 'environment.addVariable' }));
  await waitFor(() => expect(change.mock.calls.at(-1)?.[0].environment_overrides).toEqual([]));
});
