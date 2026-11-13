// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentConfig, type AgentConfigApi } from '@/lib/api/agents';
import { listModelConnections, type ModelConnection } from '@/lib/api/modelConnections';
import { providedSource } from '@/test/connectionConfig';

import { ModelsSettingsView } from './ModelsSettingsView';

vi.mock('@/lib/api/agents', () => ({ getAgentConfig: vi.fn() }));
vi.mock('@/lib/api/modelConnections', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/modelConnections')>()),
  listModelConnections: vi.fn(),
}));

function config(overrides: Partial<AgentConfigApi> = {}): AgentConfigApi {
  return {
    repos: [],
    runtimes: ['codex'],
    github_connected: true,
    github_install_url: null,
    model_connections_enabled: true,
    ...overrides,
  };
}

const own: ModelConnection = {
  id: 'mconn_01234567890123456789012345678901',
  name: 'My provider',
  base_url: 'https://api.example.com/v1',
  models: ['own-model'],
  enabled: true,
  has_api_key: true,
  runtimes: ['codex'],
  model_status: [],
  created_at: '2026-10-27T00:00:00Z',
  updated_at: '2026-10-27T00:00:00Z',
};

function providing(...sources: ReturnType<typeof providedSource>[]) {
  vi.mocked(getAgentConfig).mockResolvedValue(config({ provided_sources: sources }));
}

function provided() {
  return screen.findByRole('region', { name: 'Available models' });
}

function connections() {
  return screen.findByRole('region', { name: 'Personal model connections' });
}

/** The heading that folds the owner's connections, where there is one. */
function fold(section: HTMLElement) {
  return within(section).queryByRole('button', { name: 'Personal model connections' });
}

beforeEach(() => {
  vi.mocked(getAgentConfig).mockReset();
  vi.mocked(listModelConnections).mockReset();
  vi.mocked(listModelConnections).mockResolvedValue({
    configured: true,
    enabled: true,
    checks_per_hour: 20,
    max_models: 8,
    connections: [own],
  });
  window.location.hash = '';
});
afterEach(cleanup);

describe('the models a deployment provides, in Settings', () => {
  it('lists each by name and nothing else', async () => {
    providing(
      providedSource({
        'house-fast': [1500, 1458],
        'house-open': [null, 2300],
        'house-spent': [20, 20],
      }),
    );
    render(<ModelsSettingsView />);

    const card = await provided();
    expect(
      within(card)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual(['house-fast', 'house-open', 'house-spent']);
    // Allowances, today's use and the reset belong to the model picker.
    expect(card).not.toHaveTextContent(/1,500|1,458|42|2,300|Unlimited|20/);
    expect(within(card).queryByRole('table')).not.toBeInTheDocument();
    // One source needs no name, and the list no heading.
    expect(within(card).queryByText('House models')).not.toBeInTheDocument();
    expect(within(card).queryByRole('heading')).not.toBeInTheDocument();
    // Where a model comes from is not said anywhere on the page.
    expect(document.body).not.toHaveTextContent(/deployment|provided/i);
  });

  it('has nothing to edit', async () => {
    providing(providedSource({ 'house-fast': [100, 1] }));
    render(<ModelsSettingsView />);
    const card = await provided();
    for (const role of ['button', 'textbox', 'combobox', 'checkbox', 'switch', 'link']) {
      expect(within(card).queryByRole(role)).not.toBeInTheDocument();
    }
  });

  it('names the sources when there are several, and leaves out one that offers no model', async () => {
    const second = { id: 'oconn_ffffffffffffffffffffffffffffffff', is_default: false };
    providing(
      providedSource({ 'shared-name': [100, 1] }),
      providedSource({ 'shared-name': [5, 0] }, { ...second, name: 'Second house' }),
      providedSource(
        {},
        { ...second, id: 'oconn_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee', name: 'Empty' },
      ),
    );
    render(<ModelsSettingsView />);

    const card = await provided();
    expect(
      within(card)
        .getAllByRole('heading', { level: 2 })
        .map((heading) => heading.textContent),
    ).toEqual(['House models', 'Second house']);
    expect(within(card).getByRole('list', { name: 'House models' })).toHaveTextContent(
      'shared-name',
    );
    expect(within(card).getByRole('list', { name: 'Second house' })).toHaveTextContent(
      'shared-name',
    );
    expect(within(card).queryByText('Empty')).not.toBeInTheDocument();
  });
});

describe('the owner’s own connections, beneath provided models', () => {
  it('start folded, and open and close from their heading', async () => {
    providing(providedSource({ 'house-fast': [100, 1] }));
    render(<ModelsSettingsView />);

    const section = await connections();
    const heading = fold(section)!;
    expect(heading).toHaveAttribute('aria-expanded', 'false');
    expect(within(section).queryByText('My provider')).not.toBeInTheDocument();
    // Folded, the section asks the server for nothing.
    expect(listModelConnections).not.toHaveBeenCalled();
    expect(within(section).queryByRole('button', { name: 'Add connection' })).toBeNull();

    fireEvent.click(heading);
    expect(heading).toHaveAttribute('aria-expanded', 'true');
    expect(await within(section).findByText('My provider')).toBeInTheDocument();
    expect(within(section).getByRole('button', { name: 'Add connection' })).toBeInTheDocument();

    fireEvent.click(heading);
    expect(heading).toHaveAttribute('aria-expanded', 'false');
    expect(within(section).queryByText('My provider')).not.toBeInTheDocument();
  });

  it('arrive open from a link to them', async () => {
    providing(providedSource({ 'house-fast': [100, 1] }));
    window.location.hash = '#settings-connections';
    render(<ModelsSettingsView />);

    const section = await connections();
    await waitFor(() => expect(fold(section)).toHaveAttribute('aria-expanded', 'true'));
    expect(await within(section).findByText('My provider')).toBeInTheDocument();
  });

  it('come after the provided models', async () => {
    providing(providedSource({ 'house-fast': [100, 1] }));
    render(<ModelsSettingsView />);
    const [first, second] = [await provided(), await connections()];
    expect(first.compareDocumentPosition(second) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe('where nothing is provided', () => {
  async function unfolded() {
    render(<ModelsSettingsView />);
    const section = await connections();
    expect(fold(section)).toBeNull();
    expect(await within(section).findByText('My provider')).toBeInTheDocument();
    expect(within(section).getByRole('button', { name: 'Add connection' })).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Available models' })).not.toBeInTheDocument();
  }

  it('the connections are the page, with no fold', async () => {
    providing();
    await unfolded();
  });

  it('is the same on a control plane that predates provided models', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    await unfolded();
  });

  it('is the same when what is provided cannot be read', async () => {
    vi.mocked(getAgentConfig).mockRejectedValue(new Error('offline'));
    await unfolded();
  });

  it('is the same where every source offers this account no model', async () => {
    providing(providedSource({}));
    await unfolded();
  });

  it('shows no provided model where model connections are switched off', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        model_connections_enabled: false,
        provided_sources: [providedSource({ 'house-fast': [100, 1] })],
      }),
    );
    await unfolded();
  });
});

describe('until it is known what is provided', () => {
  it('draws neither section, so the connections never fold under the reader', async () => {
    let answer!: (value: AgentConfigApi) => void;
    vi.mocked(getAgentConfig).mockReturnValue(new Promise((resolve) => (answer = resolve)));
    render(<ModelsSettingsView />);

    expect(screen.getByRole('heading', { level: 1, name: 'Models' })).toBeInTheDocument();
    expect(screen.queryByRole('region')).not.toBeInTheDocument();
    expect(listModelConnections).not.toHaveBeenCalled();

    answer(config({ provided_sources: [providedSource({ 'house-fast': [100, 1] })] }));
    expect(fold(await connections())).toHaveAttribute('aria-expanded', 'false');
  });
});
