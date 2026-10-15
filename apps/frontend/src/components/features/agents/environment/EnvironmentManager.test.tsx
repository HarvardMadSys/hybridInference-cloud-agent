// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { getAgentConfig } from '@/lib/api/agents';
import {
  copyEnvironment,
  deleteEnvironment,
  deleteEnvironmentSnapshot,
  listEnvironments,
  rebuildEnvironmentSnapshot,
  setEnvironmentDefault,
} from '@/lib/api/environments';
import type { EnvironmentConfig } from '@/lib/api/environments';
import { listTeams } from '@/lib/api/teams';
import { EnvironmentManager } from './EnvironmentManager';
vi.mock('@/components/providers', () => ({ useT: () => (key: string) => key }));
vi.mock('@/lib/api/agents', () => ({ getAgentConfig: vi.fn() }));
vi.mock('@/lib/api/environments', () => ({
  copyEnvironment: vi.fn(),
  deleteEnvironment: vi.fn(),
  deleteEnvironmentSnapshot: vi.fn(),
  listEnvironments: vi.fn(),
  rebuildEnvironmentSnapshot: vi.fn(),
  setEnvironmentDefault: vi.fn(),
}));
vi.mock('@/lib/api/teams', () => ({ listTeams: vi.fn() }));
vi.mock('./EnvironmentDialog', async (actual) => ({
  OwnerSelect: (await actual<typeof import('./EnvironmentDialog')>()).OwnerSelect,
  EnvironmentDialog: ({
    initial,
    teams,
  }: {
    initial: { name: string; repo: string | null };
    teams?: { name: string }[];
  }) => (
    <div
      role="dialog"
      aria-label={`editing ${initial.repo === null ? 'globals' : initial.name}`}
      data-teams={(teams ?? []).map((team) => team.name).join(',')}
    />
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
  vi.mocked(listTeams).mockResolvedValue([]);
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

it('shows which environments skip installation and lets the owner rebuild or delete', async () => {
  vi.mocked(listEnvironments).mockResolvedValue({
    globals: environment({ id: 'global', repo: null, name: 'Global', revision: 2 }),
    snapshots_enabled: true,
    environments: [
      environment({
        setup_script: 'npm ci',
        snapshot: { id: 's', state: 'ready', created_at: '2026-10-15T00:00:00Z' },
      }),
      environment({ id: 'ci', name: 'Integration', setup_script: 'npm ci', snapshot: null }),
      environment({ id: 'plain', name: 'Plain' }),
    ],
  });
  vi.mocked(rebuildEnvironmentSnapshot).mockResolvedValue({ snapshot_id: 's2', base_sha: 'c' });
  vi.mocked(deleteEnvironmentSnapshot).mockResolvedValue(undefined);
  render(<EnvironmentManager />);
  const web = (await screen.findByText('org/web')).parentElement!;
  expect(within(web).getByText('environment.snapshot.ready')).toBeInTheDocument();
  expect(within(web).getByText('environment.snapshot.none')).toBeInTheDocument();
  // An environment with nothing to install has nothing to prebuild.
  expect(within(web).getAllByText(/environment.snapshot/)).toHaveLength(2);
  fireEvent.click(screen.getAllByRole('button', { name: 'environment.manageItem' })[0]);
  fireEvent.click(screen.getByRole('menuitem', { name: 'environment.snapshot.delete' }));
  await waitFor(() => expect(deleteEnvironmentSnapshot).toHaveBeenCalledWith('env'));
  fireEvent.click(screen.getAllByRole('button', { name: 'environment.manageItem' })[1]);
  expect(screen.queryByRole('menuitem', { name: 'environment.snapshot.delete' })).toBeNull();
  fireEvent.click(screen.getByRole('menuitem', { name: 'environment.snapshot.rebuild' }));
  await waitFor(() => expect(rebuildEnvironmentSnapshot).toHaveBeenCalledWith('ci'));
});

it('says nothing about snapshots where the deployment keeps none', async () => {
  render(<EnvironmentManager />);
  await screen.findByText('org/web');
  expect(screen.queryByText(/environment.snapshot/)).toBeNull();
});

const team = (id: string, name: string, role: 'owner' | 'admin' | 'member') => ({
  id,
  name,
  role,
  owner_user_id: 'alice',
  revision: 1,
  member_count: 3,
  created_at: '2026-10-15T00:00:00Z',
});

it("lets a member view, copy and choose a team's environment but not change it", async () => {
  vi.mocked(listEnvironments).mockResolvedValue({
    globals: environment({ id: 'global', repo: null, name: 'Global', revision: 2 }),
    snapshots_enabled: true,
    environments: [
      environment({
        id: 'shared',
        name: 'Shared',
        team: { id: 'team-1', name: 'Platform' },
        can_edit: false,
        setup_script: 'npm ci',
        snapshot: { id: 's', state: 'ready', created_at: '2026-10-15T00:00:00Z' },
      }),
    ],
  });
  vi.mocked(listTeams).mockResolvedValue([team('team-1', 'Platform', 'member')]);
  render(<EnvironmentManager />);
  const web = (await screen.findByText('org/web')).parentElement!;
  expect(within(web).getByText('environment.teamBadge')).toBeInTheDocument();
  expect(within(web).getByRole('button', { name: 'environment.viewItem' })).toBeInTheDocument();
  expect(within(web).getByRole('button', { name: 'environment.view' })).toBeInTheDocument();
  expect(within(web).queryByRole('button', { name: 'environment.edit' })).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'environment.manageItem' }));
  expect(screen.getAllByRole('menuitem').map((item) => item.textContent)).toEqual([
    'environment.copy',
    'environment.makeDefault',
  ]);
  // A member who administers no team copies only to themselves.
  fireEvent.click(screen.getByRole('menuitem', { name: 'environment.copy' }));
  expect(screen.queryByLabelText('environment.copyTo')).toBeNull();
});

it('copies into a team you administer, and offers only those teams', async () => {
  vi.mocked(listTeams).mockResolvedValue([
    team('team-1', 'Platform', 'admin'),
    team('team-2', 'Research', 'member'),
    team('team-3', 'Infra', 'owner'),
  ]);
  vi.mocked(copyEnvironment).mockResolvedValue(environment({ id: 'copy', name: 'Copied' }));
  render(<EnvironmentManager />);
  await screen.findByText('org/web');
  fireEvent.click(screen.getAllByRole('button', { name: 'environment.manageItem' })[0]);
  fireEvent.click(screen.getByRole('menuitem', { name: 'environment.copy' }));
  const target = await screen.findByLabelText('environment.copyTo');
  expect(
    within(target)
      .getAllByRole('option')
      .map((option) => option.getAttribute('value')),
  ).toEqual(['', 'team-1', 'team-3']);
  fireEvent.change(target, { target: { value: 'team-3' } });
  fireEvent.click(screen.getByRole('button', { name: 'environment.copy' }));
  await screen.findByRole('dialog', { name: 'editing Copied' });
  expect(vi.mocked(copyEnvironment).mock.calls[0][5]).toBe('team-3');
});

it('lets a new environment be created for a team you administer', async () => {
  vi.mocked(listTeams).mockResolvedValue([
    team('team-1', 'Platform', 'admin'),
    team('team-2', 'Research', 'member'),
  ]);
  render(<EnvironmentManager />);
  await screen.findByText('org/web');
  await waitFor(() => expect(listTeams).toHaveBeenCalled());
  fireEvent.click(screen.getByRole('button', { name: /environment.new/ }));
  await waitFor(() =>
    expect(screen.getByRole('dialog', { name: 'editing' })).toHaveAttribute(
      'data-teams',
      'Platform',
    ),
  );
});

it('still lists environments when teams cannot be read', async () => {
  vi.mocked(listTeams).mockRejectedValue(new Error('teams unavailable'));
  render(<EnvironmentManager />);
  expect(await screen.findByText('org/web')).toBeInTheDocument();
  expect(screen.queryByRole('alert')).toBeNull();
});
