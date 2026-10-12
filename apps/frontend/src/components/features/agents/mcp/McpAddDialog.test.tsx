// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { McpAddDialog } from './McpAddDialog';
import type { McpCatalogApi, McpImportPreviewApi, McpTemplateApi } from '@/lib/api/mcp';

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
  url: 'https://api.githubcopilot.com/mcp/',
  transport: 'streamable-http',
  auth_kind: 'static',
  notes: ['Choose a personal access token with access to the repositories you need.'],
  documentation_url: 'https://github.com/github/github-mcp-server',
};
const providers: McpTemplateApi[] = [
  {
    name: 'slack',
    label: 'Slack',
    description: 'Search conversations in your workspace.',
    url: 'https://mcp.slack.com/mcp',
    transport: 'streamable-http',
    auth_kind: 'oauth',
    notes: ['Your workspace admin must enable Slack MCP before you connect.'],
    documentation_url: 'https://docs.slack.dev/ai/slack-mcp-server/',
  },
  {
    name: 'google-drive',
    label: 'Google Drive',
    description: 'Find files and read your documents.',
    url: 'https://drive.mcp.claude.com/mcp',
    transport: 'streamable-http',
    auth_kind: 'oauth',
    notes: ['This server is provided by Anthropic. Review access before signing in to Google.'],
    documentation_url: 'https://support.claude.com/en/articles/10166901',
  },
];
const catalog: McpCatalogApi = {
  entries: [
    {
      name: 'com.example/docs',
      label: 'Docs',
      description: 'Team knowledge from the MCP directory.',
      version: '1.0.0',
      website_url: null,
      configurations: [],
    },
  ],
  total: 1,
  next_offset: null,
  synced_at: '2026-10-11T12:00:00Z',
  stale: false,
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
  it('keeps built-in presets available when the directory is synced and defaults GitHub to a token', async () => {
    vi.mocked(listMcpCatalog).mockResolvedValue(catalog);
    vi.mocked(createMcpConnection).mockResolvedValue({ id: 1, label: 'GitHub' } as never);
    const added = vi.fn();
    render(<McpAddDialog templates={[template, ...providers]} onClose={vi.fn()} onAdded={added} />);
    expect(screen.getByRole('button', { name: 'Slack' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Google Drive' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'GitHub' }));
    expect(listMcpCatalog).not.toHaveBeenCalled();
    expect(screen.queryByRole('button', { name: 'Docs' })).not.toBeInTheDocument();
    expect(screen.getByLabelText('Connect with')).toHaveValue('static');
    expect(screen.getByRole('link', { name: 'Create a token on GitHub ↗' })).toHaveAttribute(
      'href',
      'https://github.com/settings/personal-access-tokens/new',
    );
    expect(screen.getByText(template.notes[0])).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'GitHub setup guide ↗' })).toHaveAttribute(
      'href',
      template.documentation_url,
    );
    expect(screen.queryByLabelText('Run location')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Connect GitHub' })).toBeDisabled();
    const input = screen.getByLabelText('Access token');
    expect(input).toHaveAttribute('type', 'password');
    fireEvent.change(input, { target: { value: '  private-token  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect GitHub' }));
    await waitFor(() =>
      expect(createMcpConnection).toHaveBeenCalledWith({
        label: 'GitHub',
        transport: 'streamable-http',
        location: 'remote',
        url: template.url,
        auth_kind: 'static',
        template: 'github',
        secret: 'private-token',
        header_name: 'Authorization',
        header_scheme: 'Bearer',
      }),
    );
    expect(added).toHaveBeenCalledWith([{ id: 1, label: 'GitHub' }]);
  });

  it('clears the token when switching connection methods', async () => {
    render(
      <McpAddDialog
        templates={[{ ...template, browser_sign_in_available: true }]}
        onClose={vi.fn()}
        onAdded={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'GitHub' }));
    fireEvent.change(screen.getByLabelText('Access token'), { target: { value: 'private-token' } });
    fireEvent.change(screen.getByLabelText('Connect with'), { target: { value: 'oauth' } });
    fireEvent.change(screen.getByLabelText('Connect with'), { target: { value: 'static' } });
    expect(screen.getByLabelText('Access token')).toHaveValue('');
    expect(screen.getByRole('button', { name: 'Connect GitHub' })).toBeDisabled();
  });

  it('opens GitHub browser sign-in without requesting tokens or app credentials', async () => {
    vi.mocked(createMcpConnection).mockResolvedValue({ id: 1, label: 'GitHub' } as never);
    render(
      <McpAddDialog
        templates={[{ ...template, auth_kind: 'oauth', browser_sign_in_available: true }]}
        onClose={vi.fn()}
        onAdded={vi.fn()}
      />,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'GitHub' }));
    expect(screen.getByLabelText('Connect with')).toHaveValue('oauth');
    expect(screen.getByText(/No token or app setup needed/)).toBeInTheDocument();
    expect(screen.queryByLabelText('Access token')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('link', { name: 'Create a token on GitHub ↗' }),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Sign in with GitHub' }));
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
  });

  it.each(providers)('shows $label provider setup guidance and uses OAuth', async (provider) => {
    vi.mocked(createMcpConnection).mockResolvedValue({ id: 2, label: provider.label } as never);
    render(<McpAddDialog templates={providers} onClose={vi.fn()} onAdded={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: provider.label }));
    expect(screen.getByText(provider.notes[0])).toBeInTheDocument();
    expect(screen.getByRole('link', { name: `${provider.label} setup guide ↗` })).toHaveAttribute(
      'href',
      provider.documentation_url,
    );
    expect(screen.queryByLabelText('Access token')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: `Connect ${provider.label}` }));
    await waitFor(() =>
      expect(createMcpConnection).toHaveBeenCalledWith({
        label: provider.label,
        transport: 'streamable-http',
        location: 'remote',
        url: provider.url,
        auth_kind: 'oauth',
        template: provider.name,
      }),
    );
  });

  it('browses synced servers in a separate directory tab', async () => {
    vi.mocked(listMcpCatalog).mockResolvedValue(catalog);
    render(<McpAddDialog templates={[template]} onClose={vi.fn()} onAdded={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'MCP directory' }));
    expect(await screen.findByRole('button', { name: 'Docs' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'GitHub' })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Search MCP servers'), {
      target: { value: 'knowledge' },
    });
    await waitFor(() => expect(listMcpCatalog).toHaveBeenLastCalledWith('knowledge', 0, false));
    fireEvent.click(screen.getByRole('button', { name: 'MCP servers' }));
    expect(screen.getByRole('button', { name: 'GitHub' })).toBeInTheDocument();
  });

  it('uses the same preset connection flow when the directory has not synced', async () => {
    vi.mocked(createMcpConnection).mockResolvedValue({ id: 1, label: 'GitHub' } as never);
    render(
      <McpAddDialog
        templates={[template]}
        initialMode="directory"
        onClose={vi.fn()}
        onAdded={vi.fn()}
      />,
    );
    fireEvent.click(await screen.findByRole('button', { name: 'GitHub' }));
    expect(screen.getByLabelText('Connect with')).toHaveValue('static');
    fireEvent.change(screen.getByLabelText('Access token'), { target: { value: 'private-token' } });
    fireEvent.click(screen.getByRole('button', { name: 'Connect GitHub' }));
    await waitFor(() =>
      expect(createMcpConnection).toHaveBeenCalledWith(
        expect.objectContaining({ template: 'github', secret: 'private-token' }),
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

  it('keeps the pasted configuration when switching among all add methods', async () => {
    vi.mocked(listMcpCatalog).mockResolvedValue(catalog);
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
    fireEvent.click(screen.getByRole('button', { name: 'MCP directory' }));
    await screen.findByRole('button', { name: 'Docs' });
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
