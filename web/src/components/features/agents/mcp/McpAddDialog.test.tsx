// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { McpAddDialog } from './McpAddDialog';
import type { McpTemplateApi } from '@/lib/api/mcp';

vi.mock('@/lib/api/mcp', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/mcp')>();
  return {
    ...actual,
    createMcpConnection: vi.fn(),
    previewMcpImport: vi.fn(),
    saveMcpImport: vi.fn(),
  };
});

import { createMcpConnection, previewMcpImport, saveMcpImport } from '@/lib/api/mcp';

const mockedCreate = createMcpConnection as unknown as ReturnType<typeof vi.fn>;
const mockedPreview = previewMcpImport as unknown as ReturnType<typeof vi.fn>;
const mockedSave = saveMcpImport as unknown as ReturnType<typeof vi.fn>;

const template: McpTemplateApi = {
  name: 'github',
  label: 'GitHub',
  description: 'Read issues and pull requests.',
  url: 'https://api.githubcopilot.com/mcp/readonly',
  transport: 'streamable-http',
  auth_kind: 'oauth',
  notes: ['A client id is required before the browser flow can start.'],
};

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe('McpAddDialog', () => {
  it('prefills a connection from a template and shows its notes', async () => {
    mockedCreate.mockResolvedValue({ id: 1, label: 'GitHub' });

    render(<McpAddDialog templates={[template]} onClose={vi.fn()} onAdded={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'GitHub' }));
    expect(
      screen.getByText('A client id is required before the browser flow can start.'),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Add connection' }));

    await waitFor(() =>
      expect(mockedCreate).toHaveBeenCalledWith({
        label: 'GitHub',
        transport: 'streamable-http',
        location: 'remote',
        url: 'https://api.githubcopilot.com/mcp/readonly',
        auth_kind: 'oauth',
        template: 'github',
        secret: undefined,
        header_name: undefined,
        header_scheme: undefined,
      }),
    );
  });

  it('shows the command vector and masks an environment secret before saving', async () => {
    mockedCreate.mockResolvedValue({ id: 2, label: 'Files' });

    render(<McpAddDialog templates={[]} onClose={vi.fn()} onAdded={vi.fn()} />);

    fireEvent.change(screen.getByLabelText('Label'), { target: { value: 'Files' } });
    fireEvent.change(screen.getByLabelText('Run location'), {
      target: { value: 'managed-worker' },
    });
    fireEvent.change(screen.getByLabelText('Command'), { target: { value: 'npx' } });
    fireEvent.change(screen.getByLabelText('Arguments, space separated'), {
      target: { value: '-y server-filesystem /workspace' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Add environment secret' }));
    fireEvent.change(screen.getByLabelText('Environment variable 1 name'), {
      target: { value: 'GITHUB_TOKEN' },
    });
    fireEvent.change(screen.getByLabelText('Environment variable 1 value'), {
      target: { value: 'abcd1234' },
    });

    // The confirmation block is the user's last look: the argv that will run,
    // and the secret only as dots.
    expect(screen.getByText('npx -y server-filesystem /workspace')).toBeInTheDocument();
    expect(screen.getByText('GITHUB_TOKEN = ••••••••')).toBeInTheDocument();
    expect(screen.queryByText(/abcd1234/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Add connection' }));

    await waitFor(() => expect(mockedCreate).toHaveBeenCalled());
    expect(mockedCreate.mock.calls[0][0]).toMatchObject({
      label: 'Files',
      transport: 'stdio',
      location: 'managed-worker',
      launch: {
        command: 'npx',
        args: ['-y', 'server-filesystem', '/workspace'],
        env: { GITHUB_TOKEN: 'secret://GITHUB_TOKEN' },
      },
      env_secrets: { GITHUB_TOKEN: 'abcd1234' },
    });
  });

  it('previews an import, then saves the same paste with an overridden location', async () => {
    mockedPreview.mockResolvedValue({
      drafts: [
        {
          label: 'files',
          transport: 'stdio',
          location: 'managed-worker',
          url: null,
          argv: ['npx', '-y', 'server-filesystem'],
          env_names: ['ROOT'],
          headers: [],
          secrets: [{ name: 'TOKEN', target: 'Authorization', value: 'ab******yz' }],
          paths: ['/workspace'],
          package: null,
          warnings: ['This server runs a local process.'],
        },
      ],
      rejected: [{ name: 'broken', reason: 'no command' }],
      dialect: 'json',
    });
    mockedSave.mockResolvedValue({
      drafts: [{ id: 5, label: 'files' }],
      rejected: [],
      dialect: 'json',
    });

    render(<McpAddDialog templates={[]} onClose={vi.fn()} onAdded={vi.fn()} />);

    fireEvent.click(screen.getByRole('button', { name: 'Import' }));
    fireEvent.change(screen.getByLabelText('MCP configuration'), {
      target: { value: '{"mcpServers":{"files":{}}}' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));

    expect(await screen.findByText('npx -y server-filesystem')).toBeInTheDocument();
    expect(screen.getByText('TOKEN → Authorization = ab******yz')).toBeInTheDocument();
    expect(screen.getByText('This server runs a local process.')).toBeInTheDocument();
    expect(screen.getByText('broken: no command')).toBeInTheDocument();
    expect(mockedPreview).toHaveBeenCalledWith('{"mcpServers":{"files":{}}}', undefined);

    fireEvent.change(screen.getByLabelText('Run location for files'), {
      target: { value: 'remote' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save 1 connection' }));

    await waitFor(() =>
      expect(mockedSave).toHaveBeenCalledWith('{"mcpServers":{"files":{}}}', { files: 'remote' }),
    );
  });

  it('drops a stale preview when the paste is edited', async () => {
    mockedPreview.mockResolvedValue({
      drafts: [],
      rejected: [],
      dialect: 'json',
    });

    render(<McpAddDialog templates={[]} onClose={vi.fn()} onAdded={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Import' }));
    const textarea = screen.getByLabelText('MCP configuration');
    fireEvent.change(textarea, { target: { value: '{"mcpServers":{}}' } });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await screen.findByText('Read as json. 0 servers found.');

    fireEvent.change(textarea, { target: { value: '{"mcpServers":{"a":{}}}' } });

    expect(screen.queryByText('Read as json. 0 servers found.')).not.toBeInTheDocument();
  });
});
