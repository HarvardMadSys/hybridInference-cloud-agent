// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { McpAddDialog } from './McpAddDialog';
import type { McpImportPreviewApi, McpTemplateApi } from '@/lib/api/mcp';

vi.mock('@/lib/api/mcp', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/mcp')>()),
  createMcpConnection: vi.fn(),
  listMcpCatalog: vi.fn(),
  previewMcpImport: vi.fn(),
  saveMcpImport: vi.fn(),
}));
import {
  createMcpConnection,
  listMcpCatalog,
  previewMcpImport,
  saveMcpImport,
} from '@/lib/api/mcp';

const template: McpTemplateApi = {
  name: 'github',
  label: 'GitHub',
  description: 'Read issues and pull requests.',
  url: 'https://api.githubcopilot.com/mcp/readonly',
  transport: 'streamable-http',
  auth_kind: 'oauth',
  notes: ['Register a client ID to use OAuth.'],
};
const preview: McpImportPreviewApi = {
  dialect: 'json',
  rejected: [],
  drafts: [
    {
      label: 'docs',
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
};
const text = '{"mcpServers":{"docs":{"url":"https://docs.example.com/mcp"}}}';

beforeEach(() => {
  vi.mocked(listMcpCatalog).mockResolvedValue({
    entries: [],
    total: 0,
    next_offset: null,
    synced_at: null,
    stale: true,
  });
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('McpAddDialog', () => {
  it('connects a preset without asking for transport or location', async () => {
    vi.mocked(createMcpConnection).mockResolvedValue({ id: 1, label: 'GitHub' } as never);
    const added = vi.fn();
    render(<McpAddDialog templates={[template]} onClose={vi.fn()} onAdded={added} />);
    fireEvent.click(await screen.findByRole('button', { name: 'GitHub' }));
    expect(screen.getByText('Register a client ID to use OAuth.')).toBeInTheDocument();
    expect(screen.queryByLabelText('Run location')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Connect GitHub' }));
    await waitFor(() =>
      expect(createMcpConnection).toHaveBeenCalledWith({
        label: 'GitHub',
        transport: 'streamable-http',
        location: 'remote',
        url: template.url,
        auth_kind: 'oauth',
        template: 'github',
      }),
    );
    expect(added).toHaveBeenCalledWith([{ id: 1, label: 'GitHub' }]);
  });

  it('lets a GitHub user connect with a token instead of registering an OAuth app', async () => {
    vi.mocked(createMcpConnection).mockResolvedValue({ id: 1, label: 'GitHub' } as never);
    render(<McpAddDialog templates={[template]} onClose={vi.fn()} onAdded={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'GitHub' }));
    fireEvent.change(screen.getByLabelText('Connect with'), { target: { value: 'static' } });
    expect(screen.getByRole('button', { name: 'Connect GitHub' })).toBeDisabled();
    const input = screen.getByLabelText('Access token');
    expect(input).toHaveAttribute('type', 'password');
    fireEvent.change(input, { target: { value: 'private-token' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect GitHub' }));
    await waitFor(() =>
      expect(createMcpConnection).toHaveBeenCalledWith(
        expect.objectContaining({
          auth_kind: 'static',
          secret: 'private-token',
          header_name: 'Authorization',
          header_scheme: 'Bearer',
        }),
      ),
    );
  });

  it('validates JSON while typing and passes saved connections on for discovery', async () => {
    vi.mocked(previewMcpImport).mockResolvedValue(preview);
    vi.mocked(saveMcpImport).mockResolvedValue({
      ...preview,
      saved: [{ id: 5, label: 'docs' }] as never,
    });
    const added = vi.fn();
    render(
      <McpAddDialog templates={[template]} initialMode="json" onClose={vi.fn()} onAdded={added} />,
    );
    fireEvent.change(screen.getByLabelText('MCP configuration'), { target: { value: text } });
    fireEvent.click(await screen.findByRole('button', { name: 'Add 1 server' }));
    await waitFor(() =>
      expect(saveMcpImport).toHaveBeenCalledWith(text, { docs: 'remote' }, {}, undefined, {}),
    );
    expect(added).toHaveBeenCalledWith([{ id: 5, label: 'docs' }]);
    expect(screen.queryByLabelText('Run location for docs')).not.toBeInTheDocument();
  });

  it('shows exact command arguments and asks for unresolved credentials before adding', async () => {
    const command = {
      ...preview.drafts[0],
      transport: 'stdio' as const,
      location: 'user-connector' as const,
      url: null,
      argv: ['npx', '-y', 'package', '/my folder'],
      env_names: ['API_KEY'],
      secrets: [{ name: 'API_KEY', target: 'env', value: '(not provided)' }],
    };
    vi.mocked(previewMcpImport).mockResolvedValue({ ...preview, drafts: [command] });
    vi.mocked(saveMcpImport).mockResolvedValue({
      ...preview,
      saved: [{ id: 5, label: 'docs' }] as never,
    });
    render(<McpAddDialog templates={[]} initialMode="json" onClose={vi.fn()} onAdded={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('MCP configuration'), { target: { value: text } });
    const add = await screen.findByRole('button', { name: 'Add 1 server' });
    expect(add).toBeDisabled();
    expect(screen.getByText('npx -y package /my folder')).toBeInTheDocument();
    expect(screen.queryByRole('option', { name: 'Remote server' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Run location for docs'), {
      target: { value: 'managed-worker' },
    });
    fireEvent.change(screen.getByLabelText('docs · Value for API_KEY'), {
      target: { value: 'private-token' },
    });
    fireEvent.click(add);
    await waitFor(() =>
      expect(saveMcpImport).toHaveBeenCalledWith(text, { docs: 'managed-worker' }, {}, undefined, {
        docs: { API_KEY: 'private-token' },
      }),
    );
  });

  it('ignores an old preview response after the JSON has changed', async () => {
    let resolve!: (value: McpImportPreviewApi) => void;
    vi.mocked(previewMcpImport).mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    vi.mocked(previewMcpImport).mockResolvedValue({
      ...preview,
      drafts: [],
      rejected: [{ name: 'broken', reason: 'Missing URL' }],
    });
    render(<McpAddDialog templates={[]} initialMode="json" onClose={vi.fn()} onAdded={vi.fn()} />);
    const input = screen.getByLabelText('MCP configuration');
    fireEvent.change(input, { target: { value: text } });
    await waitFor(() => expect(previewMcpImport).toHaveBeenCalledTimes(1));
    fireEvent.change(input, { target: { value: '{"mcpServers":{"broken":{}}}' } });
    resolve(preview);
    expect(await screen.findByText('broken: Missing URL')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add 1 server' })).not.toBeInTheDocument();
  });

  it('keeps a save failure in the editor without claiming the server connected', async () => {
    vi.mocked(previewMcpImport).mockResolvedValue(preview);
    vi.mocked(saveMcpImport).mockRejectedValue(new Error('Could not save'));
    const added = vi.fn();
    render(<McpAddDialog templates={[]} initialMode="json" onClose={vi.fn()} onAdded={added} />);
    fireEvent.change(screen.getByLabelText('MCP configuration'), { target: { value: text } });
    fireEvent.click(await screen.findByRole('button', { name: 'Add 1 server' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not save');
    expect(added).not.toHaveBeenCalled();
    expect(screen.getByLabelText('MCP configuration')).toHaveValue(text);
  });

  it('keeps the pasted configuration when switching between the two add methods', async () => {
    vi.mocked(previewMcpImport).mockResolvedValue(preview);
    render(
      <McpAddDialog
        templates={[template]}
        initialMode="json"
        onClose={vi.fn()}
        onAdded={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByLabelText('MCP configuration'), { target: { value: text } });
    await screen.findByRole('button', { name: 'Add 1 server' });
    fireEvent.click(screen.getByRole('button', { name: 'MCP servers' }));
    expect(screen.queryByRole('button', { name: 'Add 1 server' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'JSON configuration' }));
    expect(screen.getByLabelText('MCP configuration')).toHaveValue(text);
    expect(screen.getByRole('button', { name: 'Add 1 server' })).toBeEnabled();
  });

  it('keeps the validated configuration when selecting the same example again', async () => {
    vi.mocked(previewMcpImport).mockResolvedValue(preview);
    render(<McpAddDialog templates={[]} initialMode="json" onClose={vi.fn()} onAdded={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'URL example' }));
    await screen.findByRole('button', { name: 'Add 1 server' });
    fireEvent.click(screen.getByRole('button', { name: 'URL example' }));
    expect(screen.getByRole('button', { name: 'Add 1 server' })).toBeEnabled();
  });

  it('asks for a separate credential for each server in a batch', async () => {
    const drafts = ['wiki', 'tracker'].map((label) => ({
      ...preview.drafts[0],
      label,
      secrets: [{ name: 'Authorization', target: 'header', value: '(not provided)' }],
    }));
    vi.mocked(previewMcpImport).mockResolvedValue({ ...preview, drafts });
    vi.mocked(saveMcpImport).mockResolvedValue({ ...preview, saved: [] });
    render(<McpAddDialog templates={[]} initialMode="json" onClose={vi.fn()} onAdded={vi.fn()} />);
    fireEvent.change(screen.getByLabelText('MCP configuration'), { target: { value: text } });
    const add = await screen.findByRole('button', { name: 'Add 2 servers' });
    fireEvent.change(screen.getByLabelText('wiki · Value for Authorization'), {
      target: { value: 'wiki-token' },
    });
    expect(add).toBeDisabled();
    fireEvent.change(screen.getByLabelText('tracker · Value for Authorization'), {
      target: { value: 'tracker-token' },
    });
    fireEvent.click(add);
    await waitFor(() =>
      expect(saveMcpImport).toHaveBeenCalledWith(
        text,
        { wiki: 'remote', tracker: 'remote' },
        {},
        undefined,
        { wiki: { Authorization: 'wiki-token' }, tracker: { Authorization: 'tracker-token' } },
      ),
    );
  });
});
