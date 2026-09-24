// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createAgentJob, getAgentConfig, listRepoBranches } from '@/lib/api/agents';
import type { AgentConfigApi } from '@/lib/api/agents';

import { TaskComposer } from './TaskComposer';

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => navigation,
}));

import { listSkills } from '@/lib/api/skills';
vi.mock('@/lib/api/skills', () => ({ listSkills: vi.fn() }));

import { getMcpConnectionCapabilities, listMcpConnections } from '@/lib/api/mcp';
vi.mock('@/lib/api/mcp', () => ({
  listMcpConnections: vi.fn(),
  getMcpConnectionCapabilities: vi.fn(),
}));

import { deleteAttachment, uploadAttachment } from '@/lib/api/attachments';
vi.mock('@/lib/api/attachments', () => ({
  uploadAttachment: vi.fn(),
  deleteAttachment: vi.fn(),
}));

vi.mock('@/lib/api/agents', () => ({
  createAgentJob: vi.fn(),
  getAgentConfig: vi.fn(),
  listRepoBranches: vi.fn(),
}));

function config(overrides: Partial<AgentConfigApi> = {}): AgentConfigApi {
  return {
    repos: ['owner/repository'],
    runtimes: ['claude-code'],
    models: ['model-a'],
    github_connected: true,
    github_install_url: null,
    ...overrides,
  };
}

// Skills no longer sits in the control row: it is reached through the plus
// menu, which is where everything a task *carries* now lives.
function mcpConnection(overrides: Record<string, unknown> = {}) {
  return {
    id: 1,
    label: 'Notion',
    template: null,
    transport: 'streamable-http',
    location: 'remote',
    url: null,
    argv: [],
    auth_kind: 'oauth',
    status: 'active',
    enabled: true,
    error_kind: 'none',
    error_detail: null,
    account_label: null,
    connector_id: null,
    has_credential: true,
    secret_hint: null,
    expires_at: null,
    config_revision: 1,
    authorization_epoch: 1,
    capability_revision: 1,
    server_name: 'notion',
    server_version: '1.0.0',
    protocol_version: '2025-06-18',
    tool_count: 2,
    resource_count: 0,
    prompt_count: 0,
    support: {},
    created_at: null,
    updated_at: null,
    ...overrides,
  } as never;
}

async function openMcpSubmenu() {
  await openAttachMenu();
  fireEvent.click(await screen.findByRole('menuitem', { name: /MCP/ }));
}

function droppedFile(fullPath: string) {
  const name = fullPath.split('/').pop()!;
  return {
    isFile: true,
    isDirectory: false,
    fullPath,
    file: (ok: (file: File) => void) => ok(new File(['data'], name)),
    createReader: () => ({ readEntries: (ok: (entries: never[]) => void) => ok([]) }),
  };
}

function droppedFolder(fullPath: string, ...children: unknown[]) {
  let read = false;
  return {
    isFile: false,
    isDirectory: true,
    fullPath,
    file: () => {},
    createReader: () => ({
      readEntries: (ok: (entries: unknown[]) => void) => {
        const page = read ? [] : children;
        read = true;
        ok(page);
      },
    }),
  };
}

function folderFile(name: string, relative: string): File {
  const made = new File(['data'], name);
  Object.defineProperty(made, 'webkitRelativePath', { value: relative });
  return made;
}

// The menu item clicks this input; a test drives it directly because the
// operating system's picker is what sits between the two in a real browser.
async function pick(container: HTMLElement, files: File[]) {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
  await act(async () => {
    fireEvent.change(input, { target: { files } });
  });
}

async function openAttachMenu() {
  fireEvent.click(await screen.findByRole('button', { name: 'Add files or skills' }));
  return screen.findByRole('menu');
}

async function skillsMenuItem() {
  await openAttachMenu();
  return screen.findByRole('menuitem', { name: /Skills/ });
}

describe('TaskComposer source-control onboarding', () => {
  beforeEach(() => {
    vi.mocked(listSkills).mockResolvedValue([]);
    vi.mocked(listMcpConnections).mockResolvedValue({ connections: [], templates: [] });
    vi.mocked(getMcpConnectionCapabilities).mockResolvedValue([]);
    vi.mocked(uploadAttachment).mockReset();
    vi.mocked(deleteAttachment).mockReset();
    vi.mocked(deleteAttachment).mockResolvedValue(undefined);
    navigation.push.mockReset();
    vi.mocked(createAgentJob).mockReset();
    vi.mocked(getAgentConfig).mockReset();
    vi.mocked(listRepoBranches).mockReset();
    vi.mocked(listRepoBranches).mockResolvedValue({ default: 'dev', branches: ['dev', 'main'] });
  });

  afterEach(() => cleanup());

  it('shows inline GitHub onboarding and a disabled composer when installation is available', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        github_connected: false,
        github_install_url: 'https://github.com/apps/example/installations/new',
      }),
    );

    render(<TaskComposer />);

    expect(await screen.findByText('Connect GitHub to start')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'What should the agent do?' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Connect GitHub/ })).toHaveAttribute(
      'href',
      '/integrations',
    );
    expect(screen.getByRole('textbox')).toBeDisabled();
    expect(screen.getByRole('button', { name: /Run/ })).toBeDisabled();
    expect(screen.queryByLabelText('Repository')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Agent')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Model')).not.toBeInTheDocument();
    expect(listRepoBranches).not.toHaveBeenCalled();
  });

  it('uses friendly administrator copy when GitHub installation is unavailable', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ github_connected: false, github_install_url: null }),
    );

    render(<TaskComposer />);

    expect(await screen.findByText('GitHub setup is not available yet')).toBeInTheDocument();
    expect(screen.getByText(/Contact your administrator/)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Connect GitHub/ })).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: /View integrations/ })).toHaveAttribute(
      'href',
      '/integrations',
    );
    expect(screen.queryByText(/AGENT_/)).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).toBeDisabled();
    expect(screen.getByRole('button', { name: /Run/ })).toBeDisabled();
  });

  // Issue #47: the deployment's App failed to load and the service is retrying.
  // Nobody has anything to connect or configure, so the notice says to wait.
  it('says GitHub is temporarily unavailable instead of sending anyone to set it up', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        github_connected: false,
        github_install_url: null,
        github_load_status: 'unavailable',
      }),
    );

    render(<TaskComposer />);

    expect(await screen.findByText('GitHub is temporarily unavailable')).toBeInTheDocument();
    expect(screen.getByText(/come back on its own/)).toBeInTheDocument();
    expect(screen.queryByText('GitHub setup is not available yet')).not.toBeInTheDocument();
    expect(screen.queryByText(/Contact your administrator/)).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /View integrations/ })).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).toBeDisabled();
    expect(screen.getByRole('button', { name: /Run/ })).toBeDisabled();
  });

  it('comes back by itself once the GitHub App loads again', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(getAgentConfig)
        .mockResolvedValueOnce(
          config({
            github_connected: false,
            github_install_url: null,
            github_load_status: 'unavailable',
          }),
        )
        .mockResolvedValue(config({ github_load_status: 'ready' }));
      await act(async () => {
        render(<TaskComposer />);
      });
      expect(screen.getByText('GitHub is temporarily unavailable')).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      expect(screen.queryByText('GitHub is temporarily unavailable')).not.toBeInTheDocument();
      expect(screen.getByRole('textbox')).toBeEnabled();
      const calls = vi.mocked(getAgentConfig).mock.calls.length;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(90_000);
      });
      expect(getAgentConfig).toHaveBeenCalledTimes(calls);
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps the connected composer interactive and submits its selected values', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);

    render(<TaskComposer />);

    expect(await screen.findByLabelText('Repository')).toHaveValue('owner/repository');
    expect(await screen.findByLabelText('Branch')).toHaveValue('dev');
    expect(screen.getByLabelText('Agent')).toHaveValue('claude-code');
    expect(screen.getByLabelText('Model')).toHaveValue('model-a');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).not.toBeDisabled();

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Fix the flaky test' } });
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));

    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith({
        repo: 'owner/repository',
        task_prompt: 'Fix the flaky test',
        runtime: 'claude-code',
        model: 'model-a',
        base_ref: 'dev',
      }),
    );
    expect(navigation.push).toHaveBeenCalledWith('/ajob_1');
  });

  it('submits default skills and lets the conversation explicitly clear them', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    vi.mocked(listSkills).mockResolvedValue([
      {
        id: 9,
        name: 'Review',
        description: 'Review changes',
        revision: 2,
        source: 'created',
        source_url: null,
        source_revision: null,
        default_enabled: true,
        created_at: '',
        updated_at: '',
      },
    ]);
    render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    const skills = await skillsMenuItem();
    expect(skills).toHaveTextContent('1');
    fireEvent.click(skills);
    fireEvent.click(await screen.findByRole('switch'));
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    expect(await skillsMenuItem()).toHaveTextContent('0');
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Review the diff' } });
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));
    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith(expect.objectContaining({ skill_ids: [] })),
    );
  });

  it('lets the server resolve unchanged account defaults for a new task', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    vi.mocked(listSkills).mockResolvedValue([
      {
        id: 9,
        name: 'Review',
        description: 'Review changes',
        revision: 2,
        source: 'created',
        source_url: null,
        source_revision: null,
        default_enabled: true,
        created_at: '',
        updated_at: '',
      },
    ]);
    render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    expect(await skillsMenuItem()).toHaveTextContent('1');
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Review the diff' } });
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).not.toHaveProperty('skill_ids');
  });

  it.each(['loading', 'failed', 'empty error'])(
    'allows starting with account defaults while skills are %s',
    async (state) => {
      vi.mocked(getAgentConfig).mockResolvedValue(config());
      vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
      if (state === 'loading') {
        vi.mocked(listSkills).mockReturnValue(new Promise(() => {}));
      } else {
        vi.mocked(listSkills).mockRejectedValue(
          new Error(state === 'empty error' ? '' : 'Skills unavailable'),
        );
      }
      render(<TaskComposer />);
      await screen.findByLabelText('Branch');
      if (state !== 'loading') {
        expect(await screen.findByRole('alert')).toHaveTextContent(
          'You can still start a task with your account defaults.',
        );
      }
      expect(await skillsMenuItem()).toHaveTextContent('defaults');
      fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
      fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Review the diff' } });
      expect(screen.getByRole('button', { name: /Run/ })).toBeEnabled();
      fireEvent.click(screen.getByRole('button', { name: /Run/ }));
      await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
      expect(vi.mocked(createAgentJob).mock.calls[0][0]).not.toHaveProperty('skill_ids');
      expect(navigation.push).toHaveBeenCalledWith('/ajob_1');
    },
  );

  it('restores the skills picker after retrying a failed defaults load', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(listSkills)
      .mockRejectedValueOnce(new Error('Skills unavailable'))
      .mockResolvedValue([]);
    render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Review the diff' } });
    expect(await screen.findByRole('alert')).toHaveTextContent('Skills unavailable');
    expect(screen.getByRole('button', { name: /Run/ })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: 'Retry loading skills' }));
    expect(await skillsMenuItem()).toHaveTextContent('0');
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    await waitFor(() => expect(screen.getByRole('button', { name: /Run/ })).toBeEnabled());
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('sends the uploads it staged, and shows them in the message rather than the control row', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    vi.mocked(uploadAttachment).mockResolvedValue({
      id: 41,
      path: 'notes.md',
      size_bytes: 12,
      content_type: 'text/markdown',
    });
    const view = render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    await pick(view.container, [new File(['hello world!'], 'notes.md')]);

    expect(await screen.findByText('notes.md')).toBeInTheDocument();
    // In the composer body, with the text — not among the run settings.
    expect(screen.getByRole('textbox').closest('div')).toContainElement(
      screen.getByText('notes.md'),
    );

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Read my notes' } });
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));
    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith(
        expect.objectContaining({ attachment_ids: [41] }),
      ),
    );
  });

  it('keeps a folder’s structure and shows it as one chip', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(uploadAttachment).mockImplementation(async (_file, path) => ({
      id: path === 'specs/api.md' ? 1 : 2,
      path: path!,
      size_bytes: 4,
      content_type: 'text/plain',
    }));
    const view = render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    await pick(view.container, [
      folderFile('api.md', 'specs/api.md'),
      folderFile('flow.svg', 'specs/diagrams/flow.svg'),
    ]);

    expect(await screen.findByText('specs/')).toBeInTheDocument();
    expect(vi.mocked(uploadAttachment).mock.calls.map(([, path]) => path)).toEqual([
      'specs/api.md',
      'specs/diagrams/flow.svg',
    ]);
  });

  it('refuses an oversized file next to the file, and attaches the rest', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(uploadAttachment).mockResolvedValue({
      id: 5,
      path: 'small.txt',
      size_bytes: 4,
      content_type: 'text/plain',
    });
    const view = render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    const huge = new File(['x'], 'dump.sql');
    Object.defineProperty(huge, 'size', { value: 6 * 1024 * 1024 });
    await pick(view.container, [new File(['tiny'], 'small.txt'), huge]);

    expect(await screen.findByText('dump.sql')).toBeInTheDocument();
    expect(screen.getByText(/over 5 MB, not attached/)).toBeInTheDocument();
    expect(screen.getByText('small.txt')).toBeInTheDocument();
    // Only the accepted one was ever sent.
    expect(vi.mocked(uploadAttachment).mock.calls.map(([file]) => file.name)).toEqual([
      'small.txt',
    ]);
  });

  it('will not start a task while a file is still on its way', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    let land: (value: never) => void = () => {};
    vi.mocked(uploadAttachment).mockReturnValue(
      new Promise((resolve) => {
        land = resolve as never;
      }),
    );
    const view = render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Read my notes' } });
    await pick(view.container, [new File(['hello'], 'notes.md')]);

    // Running now would queue a job without the file the user can see.
    await waitFor(() => expect(screen.getByRole('button', { name: /Run/ })).toBeDisabled());
    await act(async () => {
      land({ id: 9, path: 'notes.md', size_bytes: 5, content_type: 'text/plain' } as never);
    });
    await waitFor(() => expect(screen.getByRole('button', { name: /Run/ })).toBeEnabled());
  });

  it('drops a removed upload from the request and from the server', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    vi.mocked(uploadAttachment).mockResolvedValue({
      id: 41,
      path: 'notes.md',
      size_bytes: 12,
      content_type: 'text/markdown',
    });
    const view = render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    await pick(view.container, [new File(['hello world!'], 'notes.md')]);
    fireEvent.click(await screen.findByRole('button', { name: 'Remove notes.md' }));

    await waitFor(() => expect(deleteAttachment).toHaveBeenCalledWith(41));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Read my notes' } });
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).not.toHaveProperty('attachment_ids');
  });

  it('attaches a folder dropped onto the composer, structure intact', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(uploadAttachment).mockImplementation(async (_file, path) => ({
      id: 3,
      path: path!,
      size_bytes: 4,
      content_type: 'text/plain',
    }));
    render(<TaskComposer />);
    const textbox = await screen.findByRole('textbox');
    const card = textbox.closest('div')!;

    // A dropped folder is absent from `DataTransfer.files`; only the entries
    // carry it, which is why the composer walks them itself.
    const entry = droppedFolder('/specs', droppedFile('/specs/api.md'));
    await act(async () => {
      fireEvent.drop(card, {
        dataTransfer: {
          types: ['Files'],
          items: [{ kind: 'file', webkitGetAsEntry: () => entry }],
          files: [],
        },
      });
    });

    expect(await screen.findByText('specs/')).toBeInTheDocument();
    expect(vi.mocked(uploadAttachment).mock.calls[0]![1]).toBe('specs/api.md');
  });

  it('lets a task call a chosen MCP server, naming its tools rather than binding it blank', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config({ mcp_runtimes: ['claude-code'] }));
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    vi.mocked(listMcpConnections).mockResolvedValue({
      connections: [mcpConnection()],
      templates: [],
    });
    vi.mocked(getMcpConnectionCapabilities).mockResolvedValue([
      { kind: 'tool', name: 'search_pages', description: '', input_schema: {}, annotations: {} },
      { kind: 'tool', name: 'create_page', description: '', input_schema: {}, annotations: {} },
      { kind: 'resource', name: 'page', description: '', input_schema: {}, annotations: {} },
    ]);
    render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    await openMcpSubmenu();
    fireEvent.click(await screen.findByRole('switch', { name: 'Notion' }));

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Sync the spec' } });
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Notion' })).toBeChecked());
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));

    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    // Every tool named at `confirm`: the control plane denies what a binding
    // omits and derives `enabled` from what is left, so an empty map would
    // arrive switched off.
    expect(vi.mocked(createAgentJob).mock.calls[0][0].mcp_bindings).toEqual([
      {
        connection_id: 1,
        enabled: true,
        tools: { search_pages: 'confirm', create_page: 'confirm' },
      },
    ]);
  });

  it('gives a task no servers unless one was chosen', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config({ mcp_runtimes: ['claude-code'] }));
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    vi.mocked(listMcpConnections).mockResolvedValue({
      connections: [mcpConnection()],
      templates: [],
    });
    render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Sync the spec' } });
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).not.toHaveProperty('mcp_bindings');
  });

  it('says a runtime cannot take MCP before the job is created, not after', async () => {
    // The API refuses this pairing with a 400; the composer has had the answer
    // in /config all along and never read it.
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ runtimes: ['codex', 'claude-code'], mcp_runtimes: ['claude-code'] }),
    );
    render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    await openAttachMenu();
    const row = await screen.findByRole('menuitem', { name: /MCP/ });
    expect(row).toHaveTextContent('codex cannot be given MCP connections');
    expect(row).toHaveAttribute('aria-disabled', 'true');
    fireEvent.click(row);
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument();
  });

  it('reports why an unusable server cannot be chosen, and offers no switch', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config({ mcp_runtimes: ['claude-code'] }));
    vi.mocked(listMcpConnections).mockResolvedValue({
      connections: [
        mcpConnection({
          id: 3,
          label: 'lwiki',
          status: 'error',
          error_kind: 'needs_authorization',
        }),
      ],
      templates: [],
    });
    render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    await openMcpSubmenu();
    expect(await screen.findByText('lwiki')).toBeInTheDocument();
    expect(screen.getByText('needs sign-in')).toBeInTheDocument();
    expect(screen.queryByRole('switch', { name: 'lwiki' })).not.toBeInTheDocument();
  });

  it('refuses to bind a server that advertises no tools', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config({ mcp_runtimes: ['claude-code'] }));
    vi.mocked(listMcpConnections).mockResolvedValue({
      connections: [mcpConnection()],
      templates: [],
    });
    vi.mocked(getMcpConnectionCapabilities).mockResolvedValue([]);
    render(<TaskComposer />);
    await screen.findByLabelText('Branch');
    await openMcpSubmenu();
    fireEvent.click(await screen.findByRole('switch', { name: 'Notion' }));
    // Binding it would arrive disabled, which reads as a toggle that did nothing.
    expect(await screen.findByText(/advertises no tools yet/)).toBeInTheDocument();
    expect(screen.getByRole('switch', { name: 'Notion' })).not.toBeChecked();
  });

  it('preselects a requested repository that is available in backend config', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ repos: ['owner/default', 'owner/requested'] }),
    );

    render(<TaskComposer initialRepo="owner/requested" />);

    expect(await screen.findByLabelText('Repository')).toHaveValue('owner/requested');
    expect(listRepoBranches).toHaveBeenCalledWith('owner/requested');
  });

  it('falls back to the first configured repository for a stale request', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ repos: ['owner/default', 'owner/another'] }),
    );

    render(<TaskComposer initialRepo="owner/deleted" />);

    expect(await screen.findByLabelText('Repository')).toHaveValue('owner/default');
    expect(listRepoBranches).toHaveBeenCalledWith('owner/default');
  });

  it('runs the task on Enter and keeps Shift+Enter a newline', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);

    render(<TaskComposer />);
    await screen.findByLabelText('Repository');

    const composer = screen.getByRole('textbox');
    fireEvent.change(composer, { target: { value: 'Fix the flaky test' } });

    // Shift+Enter and Alt+Enter stay the newline, so the browser keeps them.
    expect(fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true })).toBe(true);
    expect(fireEvent.keyDown(composer, { key: 'Enter', altKey: true })).toBe(true);
    expect(createAgentJob).not.toHaveBeenCalled();

    fireEvent.keyDown(composer, { key: 'Enter' });

    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith({
        repo: 'owner/repository',
        task_prompt: 'Fix the flaky test',
        runtime: 'claude-code',
        model: 'model-a',
        base_ref: 'dev',
      }),
    );
    expect(navigation.push).toHaveBeenCalledWith('/ajob_1');
  });

  it('leaves Enter to the IME while a candidate is being composed', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);

    render(<TaskComposer />);
    await screen.findByLabelText('Repository');

    const composer = screen.getByRole('textbox');
    fireEvent.change(composer, { target: { value: '\u4fee\u590d' } });

    // Committing a Chinese, Japanese or Korean candidate reaches the page as an
    // Enter keypress. Sending on it would cut the message off mid-word.
    fireEvent.keyDown(composer, { key: 'Enter', isComposing: true });
    fireEvent.keyDown(composer, { key: 'Enter', keyCode: 229 });
    expect(createAgentJob).not.toHaveBeenCalled();

    fireEvent.keyDown(composer, { key: 'Enter' });
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
  });

  // One chord per test: a composer that has started a run stays submitting
  // until it navigates away, so a second send from the same render is meant to
  // be a no-op and would prove nothing about the chord.
  async function expectChordSends(modifier: { metaKey?: boolean; ctrlKey?: boolean }) {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);

    render(<TaskComposer />);
    await screen.findByLabelText('Repository');

    const composer = screen.getByRole('textbox');
    fireEvent.change(composer, { target: { value: 'Fix the flaky test' } });
    fireEvent.keyDown(composer, { key: 'Enter', ...modifier });

    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    expect(navigation.push).toHaveBeenCalledWith('/ajob_1');
  }

  it('sends the task on \u2318+Enter too', () => expectChordSends({ metaKey: true }));

  it('sends the task on Ctrl+Enter too', () => expectChordSends({ ctrlKey: true }));

  it('swallows Enter on an empty task rather than growing a blank line', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());

    render(<TaskComposer />);
    await screen.findByLabelText('Repository');

    const composer = screen.getByRole('textbox');
    fireEvent.change(composer, { target: { value: '   ' } });

    // Enter is the send key on this composer and never the newline, so with
    // nothing to send it does nothing at all. Shift+Enter still breaks the line.
    expect(fireEvent.keyDown(composer, { key: 'Enter' })).toBe(false);
    expect(fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true })).toBe(true);
    expect(createAgentJob).not.toHaveBeenCalled();
  });
});

describe('TaskComposer reasoning effort', () => {
  beforeEach(() => {
    navigation.push.mockReset();
    vi.mocked(createAgentJob).mockReset();
    vi.mocked(getAgentConfig).mockReset();
    vi.mocked(listRepoBranches).mockReset();
    vi.mocked(listRepoBranches).mockResolvedValue({ default: 'dev', branches: ['dev'] });
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
  });

  afterEach(() => cleanup());

  const thinking = config({
    models: ['model-a', 'model-plain'],
    runtime_reasoning_efforts: { 'claude-code': ['low', 'medium', 'high', 'xhigh', 'max'] },
    // Read as a yes/no — does this model take the parameter — not as a
    // vocabulary. The levels offered are the platform's four.
    model_reasoning_efforts: { 'model-a': ['low', 'high', 'max'] },
  });

  it('offers the platform levels the runtime can express', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(thinking);

    render(<TaskComposer />);

    const picker = await screen.findByLabelText('Reasoning');
    const offered = Array.from(picker.querySelectorAll('option')).map((o) => o.textContent);
    // `medium` although the model's entry omits it — that entry is a yes/no.
    // `xhigh` dropped although claude-code can say it — not a platform level.
    expect(offered).toEqual(['low', 'medium', 'high', 'max']);
  });

  it('drops a platform level the runtime cannot express', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        models: ['model-a'],
        // pi's real shape: it has no `max`.
        runtimes: ['pi'],
        runtime_reasoning_efforts: { pi: ['minimal', 'low', 'medium', 'high'] },
        model_reasoning_efforts: { 'model-a': ['low', 'high', 'max'] },
      }),
    );

    render(<TaskComposer />);

    const picker = await screen.findByLabelText('Reasoning');
    const offered = Array.from(picker.querySelectorAll('option')).map((o) => o.textContent);
    expect(offered).toEqual(['low', 'medium', 'high']);
  });

  it('starts at the deepest level on offer', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(thinking);

    render(<TaskComposer />);

    expect(await screen.findByLabelText('Reasoning')).toHaveValue('max');
  });

  it('shows no control when the model takes no effort', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        models: ['model-plain'],
        runtime_reasoning_efforts: { 'claude-code': ['low', 'high'] },
        model_reasoning_efforts: {},
      }),
    );

    render(<TaskComposer />);

    await screen.findByLabelText('Model');
    expect(screen.queryByLabelText('Reasoning')).not.toBeInTheDocument();
  });

  it('shows no control against a backend that does not report efforts', async () => {
    // The upgrade path: an older control plane answers neither map, and the
    // composer must look exactly as it did before the control existed.
    vi.mocked(getAgentConfig).mockResolvedValue(config());

    render(<TaskComposer />);

    await screen.findByLabelText('Model');
    expect(screen.queryByLabelText('Reasoning')).not.toBeInTheDocument();
  });

  // A successful submit navigates away, so each of these is its own render
  // rather than two clicks on one composer.
  async function submit(effort?: string) {
    render(<TaskComposer />);
    const textarea = await screen.findByRole('textbox');
    fireEvent.change(textarea, { target: { value: 'do it' } });
    if (effort) {
      fireEvent.change(screen.getByLabelText('Reasoning'), { target: { value: effort } });
    }
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));
    await waitFor(() => expect(createAgentJob).toHaveBeenCalled());
    return vi.mocked(createAgentJob).mock.calls[0][0];
  }

  it('sends whatever the control shows, untouched', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(thinking);

    expect(await submit()).toMatchObject({ reasoning_effort: 'max' });
  });

  it('sends the chosen level', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(thinking);

    expect(await submit('low')).toMatchObject({ reasoning_effort: 'low' });
  });

  it('sends nothing when the pair offers no control', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config({ models: ['model-plain'] }));

    expect(await submit()).not.toHaveProperty('reasoning_effort');
  });

  it("falls back to the new pair's deepest when the model stops offering one", async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(thinking);

    render(<TaskComposer />);
    fireEvent.change(await screen.findByLabelText('Reasoning'), { target: { value: 'low' } });
    // model-plain takes no effort at all, so the control goes away entirely —
    // and the pending selection must not survive to be submitted.
    fireEvent.change(screen.getByLabelText('Model'), { target: { value: 'model-plain' } });

    await waitFor(() => expect(screen.queryByLabelText('Reasoning')).not.toBeInTheDocument());
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'do it' } });
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));

    await waitFor(() => expect(createAgentJob).toHaveBeenCalled());
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).not.toHaveProperty('reasoning_effort');
  });
});

// `/goal` is the one thing this composer can be told that is not a message. The
// rule that separates the two is the feature; the request it produces is the
// contract with the control plane.
describe('TaskComposer goal command', () => {
  beforeEach(() => {
    vi.mocked(listSkills).mockResolvedValue([]);
    vi.mocked(listMcpConnections).mockResolvedValue({ connections: [], templates: [] });
    vi.mocked(getMcpConnectionCapabilities).mockResolvedValue([]);
    vi.mocked(uploadAttachment).mockReset();
    vi.mocked(deleteAttachment).mockReset();
    navigation.push.mockReset();
    vi.mocked(createAgentJob).mockReset();
    vi.mocked(getAgentConfig).mockReset();
    vi.mocked(listRepoBranches).mockReset();
    vi.mocked(listRepoBranches).mockResolvedValue({ default: 'dev', branches: ['dev', 'main'] });
    vi.mocked(getAgentConfig).mockResolvedValue(config());
  });

  afterEach(() => cleanup());

  async function composer() {
    render(<TaskComposer />);
    const box = await screen.findByRole('textbox');
    await screen.findByLabelText('Branch');
    box.focus();
    return box;
  }

  function menu() {
    return screen.queryByRole('listbox', { name: 'Commands' });
  }

  it('offers the goal command from the first slash', async () => {
    const box = await composer();
    fireEvent.change(box, { target: { value: '/' } });
    expect(menu()).toBeInTheDocument();
    expect(within(menu()!).getByRole('option', { name: /Goal/ })).toBeInTheDocument();
    expect(
      within(menu()!).getByText('Set or view the goal for a long-running task'),
    ).toBeInTheDocument();
    // The box keeps the caret: the menu is completing a sentence, not taking
    // the owner away from it.
    expect(box).toHaveFocus();

    fireEvent.change(box, { target: { value: '/go' } });
    expect(menu()).toBeInTheDocument();
  });

  it('stands the menu down for text that is not a command', async () => {
    const box = await composer();
    for (const typed of ['/goals', '/goal now', 'run /', 'src/lib/api/']) {
      fireEvent.change(box, { target: { value: typed } });
      expect(menu(), typed).not.toBeInTheDocument();
    }
  });

  it('choosing the command writes it into the box and keeps the caret there', async () => {
    const box = await composer();
    fireEvent.change(box, { target: { value: '/go' } });
    fireEvent.keyDown(box, { key: 'ArrowDown' });
    fireEvent.keyDown(box, { key: 'ArrowUp' });
    fireEvent.keyDown(box, { key: 'Enter' });

    // Enter chose the command rather than sending half of one as a prompt, and
    // choosing it opened nothing: the interaction is the box.
    expect(createAgentJob).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(box).toHaveValue('/goal ');
    expect(box).toHaveFocus();

    // Escape still stands the menu down without clearing the box.
    fireEvent.change(box, { target: { value: '/go' } });
    fireEvent.keyDown(box, { key: 'Escape' });
    expect(menu()).not.toBeInTheDocument();
    expect(box).toHaveValue('/go');
  });

  it('asks for the rest of the sentence when /goal carries no objective', async () => {
    const box = await composer();
    // The token, chosen (so the menu has already handed the caret back) and
    // then submitted with nothing after it.
    fireEvent.change(box, { target: { value: '/goal ' } });
    fireEvent.keyDown(box, { key: 'Enter' });

    // No empty goal, no task, and nothing sent to the model as prose: the
    // token stays in the box, ready to be finished.
    expect(await screen.findByRole('status')).toHaveTextContent('Add what “done” means');
    expect(createAgentJob).not.toHaveBeenCalled();
    expect(navigation.push).not.toHaveBeenCalled();
    expect(box).toHaveValue('/goal ');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    // And typing the objective clears the guidance: it is about a sentence the
    // owner has not finished, not a standing warning.
    fireEvent.change(box, { target: { value: '/goal make the suite pass' } });
    expect(screen.queryByText(/Add what “done” means/)).not.toBeInTheDocument();
  });

  it('creates exactly one task for /goal <objective>, with the objective as its first turn', async () => {
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    const box = await composer();
    fireEvent.change(box, { target: { value: '/goal make the suite pass' } });
    fireEvent.keyDown(box, { key: 'Enter' });

    // One request, one task: the objective is the first turn's content *and*
    // the condition that continues it. A second call here is the bug this
    // asserts against — the old two-step draft set a goal and still needed a
    // second Enter to start anything.
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).toMatchObject({
      repo: 'owner/repository',
      task_prompt: 'make the suite pass',
      // No round bound: the field is absent rather than filled with a default.
      goal: { objective: 'make the suite pass' },
      runtime: 'claude-code',
      model: 'model-a',
      base_ref: 'dev',
    });
    expect(vi.mocked(createAgentJob).mock.calls[0][0].goal).not.toHaveProperty('max_rounds');
    expect(navigation.push).toHaveBeenCalledWith('/ajob_1');
  });

  it('runs the command from the send button too, not only from Enter', async () => {
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    const box = await composer();
    fireEvent.change(box, { target: { value: '/goal make the suite pass' } });
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));

    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).toHaveProperty(
      'task_prompt',
      'make the suite pass',
    );
  });

  it('sends a message that merely mentions the command', async () => {
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    const box = await composer();
    fireEvent.change(box, {
      target: { value: 'please read `/goal` and tell me what it does' },
    });
    fireEvent.keyDown(box, { key: 'Enter' });

    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    const sent = vi.mocked(createAgentJob).mock.calls[0][0];
    expect(sent.task_prompt).toBe('please read `/goal` and tell me what it does');
    expect(sent).not.toHaveProperty('goal');
  });

  it('keeps the command in the box when the task could not be created', async () => {
    vi.mocked(createAgentJob).mockRejectedValue(new Error('repository is not accessible'));
    const box = await composer();
    fireEvent.change(box, { target: { value: '/goal make the suite pass' } });
    fireEvent.keyDown(box, { key: 'Enter' });

    // The retry is the same command, so nothing may be thrown away.
    expect(await screen.findByRole('alert')).toHaveTextContent('repository is not accessible');
    expect(box).toHaveValue('/goal make the suite pass');
    expect(navigation.push).not.toHaveBeenCalled();
  });

  it('leaves Enter to the IME while a candidate is being composed', async () => {
    const box = await composer();
    fireEvent.change(box, { target: { value: '/goal 修复这个测试' } });
    fireEvent.keyDown(box, { key: 'Enter', isComposing: true });
    fireEvent.keyDown(box, { key: 'Enter', keyCode: 229 });

    expect(createAgentJob).not.toHaveBeenCalled();
  });
});
