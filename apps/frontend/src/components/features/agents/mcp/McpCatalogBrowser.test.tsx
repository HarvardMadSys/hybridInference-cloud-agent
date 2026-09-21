// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { McpCatalogBrowser } from './McpCatalogBrowser';
import { listMcpCatalog, previewMcpImport, saveMcpImport, type McpCatalogApi } from '@/lib/api/mcp';

vi.mock('@/lib/api/mcp', () => ({
  listMcpCatalog: vi.fn(),
  previewMcpImport: vi.fn(),
  saveMcpImport: vi.fn(),
}));

const config = JSON.stringify({ mcpServers: { Docs: { url: 'https://docs.example.com/mcp' } } });
const catalog: McpCatalogApi = {
  entries: [
    {
      name: 'com.example/docs',
      label: 'Docs',
      description: 'Team knowledge',
      version: '1.0.0',
      website_url: null,
      configurations: [
        { label: 'Main endpoint', json: config },
        { label: 'Second endpoint', json: config.replace('/mcp', '/other') },
      ],
    },
  ],
  total: 25,
  next_offset: 24,
  synced_at: '2026-09-16T12:00:00Z',
  stale: false,
};

beforeEach(() => {
  vi.mocked(listMcpCatalog).mockResolvedValue(catalog);
  vi.mocked(previewMcpImport).mockResolvedValue({
    dialect: 'json',
    rejected: [],
    drafts: [
      {
        label: 'Docs',
        transport: 'streamable-http',
        location: 'remote',
        url: 'https://docs.example.com/mcp',
        argv: [],
        env_names: [],
        headers: [],
        secrets: [],
        paths: [],
        package: null,
        warnings: [],
      },
    ],
  });
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

function show(onAdded = vi.fn()) {
  render(
    <McpCatalogBrowser
      onAdded={onAdded}
      onBusyChange={vi.fn()}
      fallback={<p>Built-in servers</p>}
    />,
  );
}

it('uses fetched publisher metadata and reviews generated configuration before adding', async () => {
  const added = vi.fn();
  vi.mocked(saveMcpImport).mockResolvedValue({
    saved: [{ id: 12, label: 'Docs' }],
    rejected: [],
  } as never);
  show(added);
  expect(await screen.findByText('com.example/docs')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Docs' }));
  expect(screen.getByLabelText('MCP configuration')).toHaveValue(config);
  expect(saveMcpImport).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByRole('button', { name: 'Add 1 server' }));
  await waitFor(() =>
    expect(saveMcpImport).toHaveBeenCalledWith(config, { Docs: 'remote' }, {}, undefined, {}),
  );
  expect(added).toHaveBeenCalledWith([{ id: 12, label: 'Docs' }]);
});

it('keeps endpoint selection and token configuration explicit', async () => {
  show();
  fireEvent.click(await screen.findByRole('button', { name: 'Docs' }));
  fireEvent.change(screen.getByLabelText('Connection method'), { target: { value: '1' } });
  expect(screen.getByLabelText('MCP configuration')).toHaveValue(config.replace('/mcp', '/other'));
  fireEvent.change(screen.getByLabelText('Authentication'), { target: { value: 'token' } });
  const body = JSON.parse(
    (screen.getByLabelText('MCP configuration') as HTMLTextAreaElement).value,
  );
  expect(body.mcpServers.Docs.headers.Authorization).toBe('Bearer ${env:MCP_TOKEN}');
  expect(body.mcpServers.Docs.url).toBe('https://docs.example.com/other');
});

it('paginates and resets to the first page for a new search', async () => {
  show();
  await waitFor(() => expect(listMcpCatalog).toHaveBeenCalledWith('', 0, true));
  fireEvent.click(screen.getByRole('button', { name: 'All servers' }));
  await waitFor(() => expect(listMcpCatalog).toHaveBeenLastCalledWith('', 0, false));
  fireEvent.click(await screen.findByRole('button', { name: 'Next' }));
  await waitFor(() => expect(listMcpCatalog).toHaveBeenLastCalledWith('', 24, false));
  fireEvent.change(screen.getByLabelText('Search MCP servers'), { target: { value: 'knowledge' } });
  await waitFor(() => expect(listMcpCatalog).toHaveBeenLastCalledWith('knowledge', 0, false));
});

it('searches the whole directory from Recommended and restores recommendations on clear', async () => {
  show();
  await waitFor(() => expect(listMcpCatalog).toHaveBeenLastCalledWith('', 0, true));
  expect(screen.getByRole('button', { name: 'Recommended' })).toHaveAttribute(
    'aria-pressed',
    'true',
  );
  expect(screen.getByText(/Selected for common coding workflows/)).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText('Search MCP servers'), {
    target: { value: 'unlisted service' },
  });
  await waitFor(() =>
    expect(listMcpCatalog).toHaveBeenLastCalledWith('unlisted service', 0, false),
  );
  fireEvent.change(screen.getByLabelText('Search MCP servers'), { target: { value: '' } });
  await waitFor(() => expect(listMcpCatalog).toHaveBeenLastCalledWith('', 0, true));
});

it('ignores an older search completing after a newer one', async () => {
  let resolveOld!: (value: McpCatalogApi) => void;
  vi.mocked(listMcpCatalog).mockImplementation((query) => {
    if (query === 'old')
      return new Promise((resolve) => {
        resolveOld = resolve;
      });
    return Promise.resolve({ ...catalog, entries: [] });
  });
  show();
  fireEvent.change(screen.getByLabelText('Search MCP servers'), { target: { value: 'old' } });
  await waitFor(() => expect(listMcpCatalog).toHaveBeenCalledWith('old', 0, false));
  fireEvent.change(screen.getByLabelText('Search MCP servers'), { target: { value: 'new' } });
  await screen.findByText(/No matching servers/);
  resolveOld(catalog);
  await waitFor(() =>
    expect(screen.queryByRole('button', { name: 'Docs' })).not.toBeInTheDocument(),
  );
});

it('falls back and retries when the catalog endpoint is unavailable', async () => {
  vi.mocked(listMcpCatalog).mockRejectedValueOnce(new Error('unavailable'));
  show();
  expect(await screen.findByText('Built-in servers')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
  expect(await screen.findByRole('button', { name: 'Docs' })).toBeInTheDocument();
});

it('shows cached results and sends complex installs to documentation', async () => {
  vi.mocked(listMcpCatalog).mockResolvedValue({
    ...catalog,
    stale: true,
    entries: [{ ...catalog.entries[0], configurations: [] }],
  });
  show();
  expect(await screen.findByText(/Showing saved directory results/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'Docs' }));
  expect(screen.getByText(/This server needs additional setup/)).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /Add 1 server/ })).not.toBeInTheDocument();
});
