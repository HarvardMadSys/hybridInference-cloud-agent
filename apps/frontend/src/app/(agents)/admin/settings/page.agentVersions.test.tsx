// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useSyncExternalStore } from 'react';

import { getAgentAdminSettings, setAgentAdminSettings } from '@/lib/api/adminSettings';
import { getRuntimeUpgrades } from '@/lib/api/adminRuntimeUpgrades';
import { UpdateControls } from './AgentVersions';
import AdminSettingsPage from './page';
import { applicationSnapshot } from './settingsTestFixtures';

const search = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  return {
    value: '',
    subscribe(listener: () => void) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    read: () => search.value,
    set(next: string) {
      search.value = next;
      for (const listener of listeners) listener();
    },
  };
});
vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(useSyncExternalStore(search.subscribe, search.read)),
}));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      user: {
        id: 'admin-a',
        role: 'free',
        agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
      },
    },
  }),
}));
vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: vi.fn(),
  setAgentAdminSettings: vi.fn(),
}));
vi.mock('@/lib/api/admin', async (original) => ({
  ...(await original<typeof import('@/lib/api/admin')>()),
  getAgentRunnerHosts: vi.fn(async () => ({
    hosts: [],
    host_setting_definitions: [],
    runtime_versions: {},
    host_settings: {},
  })),
}));
vi.mock('@/lib/api/adminArchiveSettings', async (original) => ({
  ...(await original<typeof import('@/lib/api/adminArchiveSettings')>()),
  getSandboxArchiveSettings: vi.fn(async () => null),
}));
vi.mock('@/lib/api/adminRuntimeUpgrades', async (original) => ({
  ...(await original<typeof import('@/lib/api/adminRuntimeUpgrades')>()),
  getRuntimeUpgrades: vi.fn(async () => ({ runtimes: [] })),
}));

const REGISTRY = 'Agent package registry';

function registrySnapshot() {
  return applicationSnapshot({
    definitions: [
      {
        key: 'runtime_registry_url',
        category: 'Agent versions',
        label: REGISTRY,
        description: 'The npm registry agent versions come from.',
        kind: 'string',
        default: '',
        secret: false,
      },
    ],
    values: { runtime_registry_url: '' },
  });
}

beforeEach(() => {
  vi.clearAllMocks();
  search.value = '';
  const replace = window.history.replaceState.bind(window.history);
  vi.spyOn(window.history, 'replaceState').mockImplementation((data, unused, url) => {
    replace(data, unused, url);
    search.set(window.location.search);
  });
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState(null, '', '/');
});

/** The registry's input, after opening its group for editing when it is read-only. */
async function registryField(): Promise<HTMLElement> {
  let field = await screen.findByLabelText(REGISTRY);
  if (!field.matches('input')) {
    const group = field.closest('form')!.parentElement!;
    const edit = within(group).queryByRole('button', { name: /^Edit .* settings$/ });
    if (edit) fireEvent.click(edit);
    field = screen.getByLabelText(REGISTRY);
  }
  return field;
}

describe('the agent package registry', () => {
  it('is edited on the existing Agent versions tab rather than a second one', async () => {
    const current = registrySnapshot();
    vi.mocked(getAgentAdminSettings).mockResolvedValue(current);
    vi.mocked(setAgentAdminSettings).mockResolvedValue({
      ...current,
      revision: 2,
      values: { runtime_registry_url: 'http://verdaccio:4873' },
    });
    window.history.replaceState(null, '', '/admin/settings?tab=versions');
    render(<AdminSettingsPage />);

    const input = await registryField();
    expect(screen.getAllByRole('tab', { name: 'Agent versions' })).toHaveLength(1);
    expect(screen.getByRole('tab', { name: 'Agent versions' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
    expect(screen.getByRole('region', { name: 'Where agent versions come from' })).toContainElement(
      input,
    );

    fireEvent.change(input, { target: { value: 'http://verdaccio:4873' } });
    fireEvent.submit(input.closest('form')!);
    const review = screen.queryByRole('dialog', { name: 'Review changes' });
    const apply = review && within(review).queryByRole('button', { name: 'Apply changes' });
    if (apply) fireEvent.click(apply);
    await waitFor(() =>
      expect(setAgentAdminSettings).toHaveBeenCalledWith(
        { runtime_registry_url: 'http://verdaccio:4873' },
        1,
        expect.any(AbortSignal),
      ),
    );
    // Whom an update waits for changes with the registry, so it is read again now.
    await waitFor(() => expect(getRuntimeUpgrades).toHaveBeenCalledTimes(2));
  });

  it('names the servers an update waits for before Update is pressed', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(registrySnapshot());
    vi.mocked(getRuntimeUpgrades).mockResolvedValue({
      runtimes: [],
      registry_waiting_on: ['runner-a'],
    });
    window.history.replaceState(null, '', '/admin/settings?tab=versions');
    render(<AdminSettingsPage />);

    expect(
      await screen.findByText(
        'Updates wait until every online server downloads from the saved agent package registry. Not yet: runner-a.',
      ),
    ).toBeInTheDocument();

    const item = {
      runtime: 'codex',
      current_version: '0.145.0',
      default_version: '0.145.0',
      latest_version: '0.150.0',
      latest_error: null,
      update_available: true,
      upgrade: null,
    };
    render(
      <UpdateControls item={item} busy={false} waiting onUpdate={vi.fn()} onRollback={vi.fn()} />,
    );
    expect(screen.getByRole('button', { name: 'Update to 0.150.0' })).toBeDisabled();
  });

  it('opens on the Agent versions tab from a link to its old category tab', async () => {
    vi.mocked(getAgentAdminSettings).mockResolvedValue(registrySnapshot());
    window.history.replaceState(null, '', '/admin/settings?tab=application-Agent%20versions');
    render(<AdminSettingsPage />);

    await screen.findByLabelText(REGISTRY);
    expect(screen.getByRole('tab', { name: 'Agent versions' })).toHaveAttribute(
      'aria-selected',
      'true',
    );
  });
});
