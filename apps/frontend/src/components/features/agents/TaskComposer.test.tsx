// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import {
  act,
  cleanup,
  createEvent,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  createAgentJob,
  createGitHubRepository,
  getAgentConfig,
  listAgentConversations,
  listRepoBranches,
} from '@/lib/api/agents';
import type { AgentConfigApi, AgentConversationApi, AgentJobApi } from '@/lib/api/agents';
import { getInsights, type WorkInsights } from '@/lib/api/insights';
vi.mock('@/lib/api/insights', () => ({ getInsights: vi.fn() }));
import { getUserProfile } from '@/lib/api/userProfile';
vi.mock('@/lib/api/userProfile', () => ({ getUserProfile: vi.fn() }));

import { rewritePrompt, type RewritePromptResult } from '@/lib/api/promptRewrite';

vi.mock('@/lib/api/promptRewrite', () => ({ rewritePrompt: vi.fn() }));

import { FirstRunProvider } from './firstRun';
import { requestGitHubAccess } from './githubReturn';
import { TaskComposer } from './TaskComposer';

const navigation = vi.hoisted(() => ({ push: vi.fn() }));
const account = vi.hoisted(() => ({ id: undefined as string | undefined }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: account.id ? { id: account.id } : null } }),
}));
beforeEach(() => {
  account.id = undefined;
  vi.mocked(getInsights).mockReset();
  vi.mocked(getInsights).mockRejectedValue(new Error('Avatar unavailable'));
  vi.mocked(getUserProfile).mockReset();
  vi.mocked(getUserProfile).mockResolvedValue({
    email: 'owner@example.com',
    first_name: '',
    last_name: '',
    image_data_url: null,
    revision: 0,
    companion_data_url: 'data:image/svg+xml,<svg id="fixed"></svg>',
  });
  localStorage.clear();
});

vi.mock('next/navigation', () => ({
  useRouter: () => navigation,
  usePathname: () => '/',
}));

import { getGlobalEnvironment, listEnvironments } from '@/lib/api/environments';
import type { EnvironmentConfig } from '@/lib/api/environments';
vi.mock('@/lib/api/environments', () => ({
  listEnvironments: vi.fn(),
  getGlobalEnvironment: vi.fn(),
}));

function globalEnvironment(overrides: Partial<EnvironmentConfig> = {}): EnvironmentConfig {
  return {
    id: null,
    name: 'Global',
    repo: null,
    revision: 0,
    variables: [],
    setup_script: '',
    start_script: '',
    verify_script: '',
    actions: [],
    ...overrides,
  };
}

// A temporary workspace has no repository, so its environment is the owner's
// global variables alone.
beforeEach(() => {
  vi.mocked(getGlobalEnvironment).mockReset();
  vi.mocked(getGlobalEnvironment).mockResolvedValue(globalEnvironment({ revision: 3 }));
});

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
  createGitHubRepository: vi.fn(),
  getAgentConfig: vi.fn(),
  listAgentConversations: vi.fn(),
  listRepoBranches: vi.fn(),
}));

// Leaving for GitHub is a real navigation; the test stands in for the browser.
import { githubAccessUrl, leaveFor } from './githubAccess';
vi.mock('./githubAccess', () => ({ githubAccessUrl: vi.fn(), leaveFor: vi.fn() }));

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

function conversation(archived = false): AgentConversationApi {
  return {
    job: { id: 'ajob_0', thread_id: 'thread_0' } as AgentJobApi,
    title: 'Earlier task',
    archived,
    last_activity_at: null,
  };
}

function userProfile(overrides: Partial<WorkInsights> = {}): WorkInsights {
  return {
    avatar: { data_url: 'data:image/svg+xml,<svg></svg>', style: 'illustrated' },
    focus_areas: [],
    suggestions: [
      {
        id: 'success-checks',
        title: 'Include success checks',
        reason: 'Recent requests could use a clearer definition of done.',
        action: 'Describe the result you expect and how to verify it.',
      },
    ],
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

// The composer is busy until the chosen repository's environments arrive: the
// plus menu is disabled for exactly that long, and a paste is ignored.
async function untilIdle() {
  await waitFor(() => expect(screen.getByRole('button', { name: 'Add to message' })).toBeEnabled());
}

async function openAttachMenu() {
  await untilIdle();
  fireEvent.click(screen.getByRole('button', { name: 'Add to message' }));
  return screen.findByRole('menu');
}

async function skillsMenuItem() {
  await openAttachMenu();
  return screen.findByRole('menuitem', { name: /Skills/ });
}

function repositoryButton() {
  return screen.findByRole('button', { name: /^Repository:/ });
}

function repositoryOptions(dialog: HTMLElement) {
  return within(dialog)
    .getAllByRole('button')
    .filter((button) => button.hasAttribute('data-repository-option'))
    .map((button) => button.getAttribute('aria-label'));
}

const TEMPORARY_OPTION = 'Start from scratch';
const ACKNOWLEDGED = 'agents.temporaryWorkspace.acknowledged.alice';

// Nobody connected, and a deployment App to connect to.
function disconnected(overrides: Partial<AgentConfigApi> = {}): AgentConfigApi {
  return config({
    repos: [],
    github_connected: false,
    github_install_url: '/agents/integrations',
    ...overrides,
  });
}

// Run stays disabled until the composer has its config and the repository's
// environments. A click or Enter before then sends nothing, so a test that
// sends waits for it, as a person would.
async function untilRunnable() {
  await waitFor(() => expect(screen.getByRole('button', { name: /^Run/ })).toBeEnabled());
}

async function runWhenReady() {
  await untilRunnable();
  fireEvent.click(screen.getByRole('button', { name: /^Run/ }));
}

// Nothing is chosen for the owner, so every test that runs a task picks where
// first — through the control a person would use.
async function chooseRepository(name = 'owner/repository') {
  fireEvent.click(await repositoryButton());
  const dialog = await screen.findByRole('dialog', { name: 'Repository' });
  fireEvent.click(within(dialog).getByRole('button', { name }));
  await screen.findByLabelText('Branch');
}

describe('TaskComposer source-control onboarding', () => {
  it('applies the repository default without showing environment controls in the composer', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    const globals = {
      id: 'global',
      name: 'Global',
      repo: null,
      revision: 2,
      variables: [],
      setup_script: '',
      start_script: '',
      verify_script: '',
      actions: [],
    };
    vi.mocked(listEnvironments).mockResolvedValue({
      globals,
      environments: [
        {
          ...globals,
          id: 'env',
          name: 'Development',
          repo: 'owner/repository',
          revision: 4,
          is_default: true,
        },
      ],
    });
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'new-job' } as AgentJobApi);
    render(<TaskComposer />);
    await chooseRepository();
    await waitFor(() => expect(listEnvironments).toHaveBeenCalledWith('owner/repository'));
    expect(screen.queryByRole('radio')).not.toBeInTheDocument();
    expect(screen.queryByText('What this task will use')).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox', { name: 'What should the agent do?' }), {
      target: { value: 'Fix tests' },
    });
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run' })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: 'Run' }));
    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith(
        expect.objectContaining({
          environment_id: 'env',
          environment_revision: 4,
          global_revision: 2,
        }),
      ),
    );
  });

  it('keeps the chosen environment after closing and reopening the dialog', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(listEnvironments).mockResolvedValue({
      globals: globalEnvironment({ revision: 2 }),
      environments: [
        {
          ...globalEnvironment(),
          id: 'env',
          name: 'Development',
          repo: 'owner/repository',
          revision: 4,
          is_default: true,
        },
      ],
    });
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'new-job' } as AgentJobApi);
    render(<TaskComposer />);
    await chooseRepository();
    await openAttachMenu();
    fireEvent.click(await screen.findByRole('menuitem', { name: /Environment/ }));
    const dialog = screen.getByRole('dialog', { name: 'Environment' });
    const development = await within(dialog).findByRole('radio', { name: /Development/ });
    expect(development).toBeChecked();
    expect(development).toHaveFocus();
    expect(within(dialog).queryByText('Variables for this workspace only')).toBeNull();
    fireEvent.click(within(dialog).getByRole('radio', { name: /No environment/ }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Done' }));
    expect(screen.queryByRole('dialog', { name: 'Environment' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add to message' })).toHaveFocus();
    await openAttachMenu();
    const item = screen.getByRole('menuitem', { name: /Environment/ });
    expect(item).toHaveTextContent('No environment');
    fireEvent.click(item);
    expect(screen.getByRole('radio', { name: /No environment/ })).toBeChecked();
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.change(screen.getByRole('textbox', { name: 'What should the agent do?' }), {
      target: { value: 'Fix tests' },
    });
    await runWhenReady();
    await waitFor(() => expect(createAgentJob).toHaveBeenCalled());
    const request = vi.mocked(createAgentJob).mock.calls[0][0];
    expect(request).toMatchObject({ environment_id: null, global_revision: 2 });
    expect(request).not.toHaveProperty('environment_overrides');
  });

  it('runs a scratch workspace with the global variables and no task variables', async () => {
    account.id = 'alice';
    localStorage.setItem(ACKNOWLEDGED, '1');
    vi.mocked(getAgentConfig).mockResolvedValue(disconnected());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'new-job' } as AgentJobApi);
    render(<TaskComposer />);
    expect(await repositoryButton()).toHaveAccessibleName('Repository: Start from scratch');
    await openAttachMenu();
    fireEvent.click(await screen.findByRole('menuitem', { name: /Environment/ }));
    const dialog = screen.getByRole('dialog', { name: 'Environment' });
    expect(dialog).toHaveTextContent('Start from scratch');
    expect(dialog).toHaveTextContent('cannot use a repository environment');
    expect(within(dialog).queryByRole('radio')).not.toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: 'Add variable' })).not.toBeInTheDocument();
    expect(
      within(dialog).getByRole('button', { name: 'Edit personal global variables' }),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Done' })).toHaveFocus();
    fireEvent.click(within(dialog).getByRole('button', { name: 'Done' }));
    fireEvent.change(screen.getByRole('textbox', { name: 'What should the agent do?' }), {
      target: { value: 'Write hello.py' },
    });
    await runWhenReady();
    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith(
        expect.objectContaining({ repo: null, environment_id: null, global_revision: 3 }),
      ),
    );
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).not.toHaveProperty('environment_overrides');
    expect(getGlobalEnvironment).toHaveBeenCalledOnce();
    expect(listEnvironments).not.toHaveBeenCalled();
  });

  it('offers repository environments after switching from a scratch workspace', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'new-job' } as AgentJobApi);
    render(<TaskComposer initialRepo=":temporary" />);
    expect(await repositoryButton()).toHaveAccessibleName('Repository: Start from scratch');
    await openAttachMenu();
    fireEvent.click(await screen.findByRole('menuitem', { name: /Environment/ }));
    expect(screen.queryByRole('radio')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    await chooseRepository();
    await openAttachMenu();
    fireEvent.click(await screen.findByRole('menuitem', { name: /Environment/ }));
    expect(await screen.findByRole('radio', { name: /No environment/ })).toBeInTheDocument();
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.change(screen.getByRole('textbox', { name: 'What should the agent do?' }), {
      target: { value: 'Fix tests' },
    });
    await runWhenReady();
    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith(
        expect.objectContaining({
          repo: 'owner/repository',
          environment_id: null,
          global_revision: 0,
        }),
      ),
    );
  });

  it('keeps repository switching usable after environment loading fails and can retry', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(listEnvironments).mockRejectedValueOnce(new Error('Environment unavailable'));
    render(<TaskComposer />);
    await chooseRepository();
    expect(await screen.findByText(/Environments could not be loaded/)).toBeInTheDocument();
    expect(await repositoryButton()).toBeEnabled();
    fireEvent.change(screen.getByRole('textbox', { name: 'What should the agent do?' }), {
      target: { value: 'Fix tests' },
    });
    expect(screen.getByRole('button', { name: 'Run' })).toBeEnabled();
    vi.mocked(listEnvironments).mockResolvedValueOnce({
      environments: [],
      globals: {
        id: null,
        name: 'Global',
        repo: null,
        revision: 0,
        variables: [],
        setup_script: '',
        start_script: '',
        verify_script: '',
        actions: [],
      },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run' })).toBeEnabled());
  });
  it('submits without environment fields after an initial load failure so server defaults apply', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(listEnvironments).mockRejectedValue(new Error('Environment unavailable'));
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'new-job' } as AgentJobApi);
    render(<TaskComposer />);
    await chooseRepository();
    await screen.findByText(/Environments could not be loaded/);
    fireEvent.change(screen.getByRole('textbox', { name: 'What should the agent do?' }), {
      target: { value: 'Fix tests' },
    });
    await runWhenReady();
    await waitFor(() => expect(createAgentJob).toHaveBeenCalled());
    const request = vi.mocked(createAgentJob).mock.calls[0][0];
    expect(request).not.toHaveProperty('environment_id');
    expect(request).not.toHaveProperty('environment_revision');
    expect(request).not.toHaveProperty('global_revision');
    expect(request).not.toHaveProperty('environment_overrides');
  });
  beforeEach(() => {
    sessionStorage.clear();
    vi.mocked(githubAccessUrl).mockReset();
    vi.mocked(leaveFor).mockReset();
    vi.mocked(listEnvironments)
      .mockReset()
      .mockResolvedValue({
        environments: [],
        globals: {
          id: null,
          name: 'Global',
          repo: null,
          revision: 0,
          variables: [],
          setup_script: '',
          start_script: '',
          verify_script: '',
          actions: [],
        },
      });
    vi.mocked(listSkills).mockResolvedValue([]);
    vi.mocked(listMcpConnections).mockResolvedValue({ connections: [], templates: [] });
    vi.mocked(getMcpConnectionCapabilities).mockResolvedValue([]);
    vi.mocked(uploadAttachment).mockReset();
    vi.mocked(deleteAttachment).mockReset();
    vi.mocked(deleteAttachment).mockResolvedValue(undefined);
    navigation.push.mockReset();
    vi.mocked(createAgentJob).mockReset();
    vi.mocked(createGitHubRepository).mockReset();
    vi.mocked(getAgentConfig).mockReset();
    vi.mocked(listAgentConversations).mockReset();
    vi.mocked(listRepoBranches).mockReset();
    vi.mocked(listRepoBranches).mockResolvedValue({ default: 'dev', branches: ['dev', 'main'] });
  });

  afterEach(() => cleanup());

  it('shows the avatar above the composer and user suggestions below it from one profile request', async () => {
    account.id = 'alice';
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    const profile = userProfile();
    vi.mocked(getInsights).mockResolvedValueOnce(profile);
    const rendered = render(<TaskComposer />);
    const image = await screen.findByRole('img', {
      name: 'Your Pokémon companion avatar',
    });
    const heading = screen.getByRole('heading', { name: 'What should the agent do?' });
    expect(image.compareDocumentPosition(heading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    const box = screen.getByRole('textbox', { name: 'What should the agent do?' });
    expect(box).toBeEnabled();
    const suggestions = screen.getByRole('region', { name: 'Suggestions for your next request' });
    expect(
      box.compareDocumentPosition(suggestions) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(within(suggestions).getByText(profile.suggestions[0].title)).toBeInTheDocument();
    expect(within(suggestions).getByText(profile.suggestions[0].reason)).toBeInTheDocument();
    expect(within(suggestions).getByText(profile.suggestions[0].action)).toBeInTheDocument();
    expect(within(suggestions).getByText('How your avatar is generated')).toBeInTheDocument();
    expect(getInsights).toHaveBeenCalledTimes(1);
    rendered.unmount();
  });

  it('retains the avatar and suggestions immediately when returning to New task', async () => {
    account.id = 'alice';
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    const profile = userProfile();
    vi.mocked(getInsights).mockResolvedValueOnce(profile);
    const first = render(<TaskComposer />);
    await screen.findByText(profile.suggestions[0].title);
    expect(screen.getByRole('img')).toHaveAttribute('src', profile.avatar.data_url);
    first.unmount();
    vi.mocked(getInsights).mockReturnValueOnce(new Promise(() => {}));
    vi.mocked(getUserProfile).mockReturnValueOnce(new Promise(() => {}));
    render(<TaskComposer />);
    // The composer waits for agent configuration independently of avatar reads.
    await screen.findByRole('textbox', { name: 'What should the agent do?' });
    expect(screen.getByRole('img')).toHaveAttribute('src', profile.avatar.data_url);
    expect(screen.getByText(profile.suggestions[0].title)).toBeVisible();
    expect(getInsights).toHaveBeenCalledTimes(2);
    expect(getUserProfile).toHaveBeenCalledTimes(2);
  });

  it('keeps an uncached composer usable when insights fail and allows retrying', async () => {
    account.id = 'alice';
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    const profile = userProfile();
    render(<TaskComposer />);
    expect(await screen.findByRole('textbox', { name: 'What should the agent do?' })).toBeEnabled();
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(
      await screen.findByText('Could not load your suggestions. You can still start a task.'),
    ).toBeInTheDocument();
    vi.mocked(getInsights).mockResolvedValueOnce(profile);
    fireEvent.click(
      within(screen.getByRole('region', { name: 'Suggestions for your next request' })).getByRole(
        'button',
        { name: 'Retry suggestions' },
      ),
    );
    await screen.findByText(profile.suggestions[0].title);
    expect(screen.getByRole('textbox', { name: 'What should the agent do?' })).toBeEnabled();
  });

  it('removes the previous account suggestions and ignores its pending response on account changes', async () => {
    account.id = 'alice';
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    const alice = userProfile();
    vi.mocked(getInsights).mockResolvedValueOnce(alice);
    const rendered = render(<TaskComposer />);
    await screen.findByText(alice.suggestions[0].title);

    let resolveBob!: (next: WorkInsights) => void;
    vi.mocked(getInsights).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveBob = resolve;
      }),
    );
    account.id = 'bob';
    rendered.rerender(<TaskComposer />);
    expect(screen.queryByText(alice.suggestions[0].title)).not.toBeInTheDocument();
    await waitFor(() => expect(getInsights).toHaveBeenCalledTimes(2));
    const bobSignal = vi.mocked(getInsights).mock.calls[1][0]!;

    const carol = userProfile({
      suggestions: [
        {
          id: 'carol',
          title: 'Carol’s request guidance',
          reason: 'Her recent work',
          action: 'Add verification steps',
        },
      ],
    });
    vi.mocked(getInsights).mockResolvedValueOnce(carol);
    account.id = 'carol';
    rendered.rerender(<TaskComposer />);
    await screen.findByText(carol.suggestions[0].title);
    expect(bobSignal.aborted).toBe(true);
    await act(async () => resolveBob(alice));
    expect(screen.queryByText(alice.suggestions[0].title)).not.toBeInTheDocument();
    expect(screen.getByText(carol.suggestions[0].title)).toBeInTheDocument();

    account.id = undefined;
    rendered.rerender(<TaskComposer />);
    expect(
      screen.queryByRole('region', { name: 'Suggestions for your next request' }),
    ).not.toBeInTheDocument();
    expect(getInsights).toHaveBeenCalledTimes(3);
  });

  it('rewrites the draft for review before creating a task', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'rewritten-task' } as never);
    vi.mocked(rewritePrompt).mockResolvedValue({
      kind: 'prompt',
      prompt: 'Add CSV export with accessible controls and clear error feedback.',
    });
    render(<TaskComposer initialRepo="owner/repository" />);
    const box = await screen.findByRole('textbox', { name: 'What should the agent do?' });
    fireEvent.change(box, { target: { value: 'Add export' } });
    fireEvent.click(screen.getByRole('button', { name: 'Rewrite prompt' }));
    await waitFor(() =>
      expect(box).toHaveValue('Add CSV export with accessible controls and clear error feedback.'),
    );
    expect(createAgentJob).not.toHaveBeenCalled();
    await runWhenReady();
    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith(
        expect.objectContaining({
          task_prompt: 'Add CSV export with accessible controls and clear error feedback.',
        }),
      ),
    );
  });

  it.each(['onboarding', 'repository picker'] as const)(
    'waits for a pending rewrite before opening repository creation from %s',
    async (entry) => {
      vi.mocked(getAgentConfig).mockResolvedValue(
        config({ repos: entry === 'onboarding' ? [] : ['owner/repository'] }),
      );
      let finish!: (result: RewritePromptResult) => void;
      vi.mocked(rewritePrompt).mockReturnValue(
        new Promise((resolve) => {
          finish = resolve;
        }),
      );
      render(<TaskComposer />);
      const task = await screen.findByRole('textbox', { name: 'What should the agent do?' });
      fireEvent.change(task, { target: { value: 'Build an app' } });
      fireEvent.click(screen.getByRole('button', { name: 'Rewrite prompt' }));
      if (entry === 'repository picker') fireEvent.click(await repositoryButton());
      const create = screen.getByRole('button', { name: 'Create repository' });
      expect(create).toBeDisabled();
      fireEvent.click(create);
      expect(screen.queryByRole('dialog', { name: 'Create repository' })).not.toBeInTheDocument();
      expect(createGitHubRepository).not.toHaveBeenCalled();
      await act(async () => finish({ kind: 'prompt', prompt: 'Build an accessible app' }));
      expect(task).toHaveValue('Build an accessible app');
      if (entry === 'repository picker') fireEvent.click(await repositoryButton());
      const available = screen.getByRole('button', { name: 'Create repository' });
      expect(available).toBeEnabled();
      fireEvent.click(available);
      const name = await screen.findByRole('textbox', { name: 'Repository name' });
      await waitFor(() => expect(name).toHaveFocus());
      expect(screen.getByRole('dialog', { name: 'Create repository' })).toBeInTheDocument();
      expect(createAgentJob).not.toHaveBeenCalled();
    },
  );

  it('keeps Enter from starting a task while clarification is open', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(rewritePrompt).mockResolvedValue({
      kind: 'questions',
      questions: ['Which export format?', 'Which records should be included?'],
    });
    render(<TaskComposer initialRepo="owner/repository" />);
    const box = await screen.findByRole('textbox', { name: 'What should the agent do?' });
    fireEvent.change(box, { target: { value: 'Add export' } });
    fireEvent.click(screen.getByRole('checkbox', { name: 'Ask clarification questions first' }));
    fireEvent.click(screen.getByRole('button', { name: 'Rewrite prompt' }));
    await screen.findByText('Which export format?');
    fireEvent.click(await repositoryButton());
    expect(screen.getByRole('button', { name: 'Create repository' })).toBeDisabled();
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(createAgentJob).not.toHaveBeenCalled();
    expect(box).toHaveValue('Add export');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(screen.getByRole('button', { name: /^Run/ })).toBeEnabled());
    fireEvent.click(await repositoryButton());
    expect(screen.getByRole('button', { name: 'Create repository' })).toBeEnabled();
  });

  // Someone arriving for the first time has nothing connected, and needs
  // nothing: the task runs in a temporary workspace, and connecting GitHub is
  // one optional line beside the box.
  it('runs a task in a temporary workspace before GitHub is connected', async () => {
    account.id = 'alice';
    vi.mocked(getAgentConfig).mockResolvedValue(disconnected());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);

    render(<TaskComposer />);

    expect(await repositoryButton()).toHaveAccessibleName('Repository: Start from scratch');
    // No branch to pin, and no repository to ask for one.
    expect(screen.queryByLabelText('Branch')).not.toBeInTheDocument();
    expect(listRepoBranches).not.toHaveBeenCalled();
    expect(
      screen.getByRole('button', { name: 'Connect GitHub to use a repository' }),
    ).toBeInTheDocument();
    // Named by the question above it, with no example task in it.
    const box = screen.getByRole('textbox', { name: 'What should the agent do?' });
    expect(box).toBeEnabled();
    expect(box).not.toHaveAttribute('placeholder');
    fireEvent.change(box, { target: { value: 'Plot the survey results' } });
    await waitFor(() => expect(screen.getByRole('button', { name: /^Run/ })).toBeEnabled());

    // The first one waits for its owner to read what a temporary workspace
    // keeps; the draft stays where it is meanwhile.
    fireEvent.keyDown(box, { key: 'Enter' });
    const notice = await screen.findByRole('alertdialog', { name: 'Before you start' });
    expect(notice).toHaveTextContent('not automatically saved to GitHub');
    expect(within(notice).getByRole('button', { name: 'Got it' })).toHaveFocus();
    expect(createAgentJob).not.toHaveBeenCalled();
    expect(box).toHaveValue('Plot the survey results');

    fireEvent.click(within(notice).getByRole('button', { name: 'Got it' }));
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith('/ajob_1'));
    const request = vi.mocked(createAgentJob).mock.calls[0][0];
    expect(request).toMatchObject({
      repo: null,
      task_prompt: 'Plot the survey results',
      // The owner's global variables, and no repository environment.
      environment_id: null,
      global_revision: 3,
    });
    expect(request).not.toHaveProperty('base_ref');
    expect(listEnvironments).not.toHaveBeenCalled();
    expect(localStorage.getItem(ACKNOWLEDGED)).toBe('1');
  });

  it('asks only once, then starts a temporary workspace straight away', async () => {
    account.id = 'alice';
    localStorage.setItem(ACKNOWLEDGED, '1');
    vi.mocked(getAgentConfig).mockResolvedValue(disconnected());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_2' } as never);

    render(<TaskComposer />);
    fireEvent.change(await screen.findByRole('textbox', { name: 'What should the agent do?' }), {
      target: { value: 'Draft a README' },
    });
    await runWhenReady();

    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith(expect.objectContaining({ repo: null })),
    );
    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
  });

  it('sends nothing when the notice is cancelled, and hands the caret back to the draft', async () => {
    account.id = 'alice';
    vi.mocked(getAgentConfig).mockResolvedValue(disconnected());

    render(<TaskComposer />);
    const box = await screen.findByRole('textbox', { name: 'What should the agent do?' });
    fireEvent.change(box, { target: { value: 'Draft a README' } });
    await runWhenReady();
    const notice = await screen.findByRole('alertdialog', { name: 'Before you start' });

    fireEvent.click(within(notice).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument();
    expect(createAgentJob).not.toHaveBeenCalled();
    expect(box).toHaveValue('Draft a README');
    expect(box).toHaveFocus();
    // Not read, so asked again next time.
    expect(localStorage.getItem(ACKNOWLEDGED)).toBeNull();
  });

  // Connecting is for a repository, so the trip back asks where to run the
  // task instead of keeping the temporary workspace it was leaving.
  it('connects GitHub from beside the box, and comes back asking where to run the task', async () => {
    account.id = 'alice';
    vi.mocked(getAgentConfig).mockResolvedValue(disconnected());
    vi.mocked(githubAccessUrl).mockResolvedValue('https://github.test/login/oauth/authorize');
    const view = render(<TaskComposer initialRepo="owner/repository" />);
    fireEvent.change(await screen.findByRole('textbox', { name: 'What should the agent do?' }), {
      target: { value: 'Fix the flaky test' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Connect GitHub to use a repository' }));

    // Straight to GitHub, noting where to come back to and what was written.
    await waitFor(() =>
      expect(leaveFor).toHaveBeenCalledWith('https://github.test/login/oauth/authorize'),
    );
    expect(githubAccessUrl).toHaveBeenCalledWith('connect');
    expect(JSON.parse(sessionStorage.getItem('agents.github.return')!).value).toBe(
      '/?repo=owner%2Frepository',
    );
    expect(JSON.parse(sessionStorage.getItem('agents.composer.draft:alice')!).value).toMatchObject({
      task: 'Fix the flaky test',
      repo: '',
    });

    view.unmount();
    vi.mocked(getAgentConfig).mockResolvedValue(config({ repos: ['owner/other'] }));
    render(<TaskComposer />);

    const dialog = await screen.findByRole('dialog', { name: 'Repository' });
    expect(within(dialog).getByRole('searchbox')).toHaveFocus();
    expect(screen.getByRole('textbox', { name: 'What should the agent do?' })).toHaveValue(
      'Fix the flaky test',
    );
  });

  it('comes back from GitHub to the picker, not the temporary workspaces it left', async () => {
    account.id = 'alice';
    localStorage.setItem(ACKNOWLEDGED, '1');
    vi.mocked(getAgentConfig).mockResolvedValue(disconnected());
    vi.mocked(githubAccessUrl).mockResolvedValue('https://github.test/login/oauth/authorize');
    const view = render(<TaskComposer initialRepo=":temporary" />);
    fireEvent.change(await screen.findByRole('textbox', { name: 'What should the agent do?' }), {
      target: { value: 'Fix the flaky test' },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Connect GitHub to use a repository' }));

    await waitFor(() =>
      expect(leaveFor).toHaveBeenCalledWith('https://github.test/login/oauth/authorize'),
    );
    expect(JSON.parse(sessionStorage.getItem('agents.github.return')!).value).toBe('/');

    view.unmount();
    vi.mocked(getAgentConfig).mockResolvedValue(config({ repos: ['owner/other'] }));
    render(<TaskComposer />);

    const dialog = await screen.findByRole('dialog', { name: 'Repository' });
    expect(within(dialog).getByRole('searchbox')).toHaveFocus();
  });

  it('asks where to run a task written before connecting, once back from GitHub', async () => {
    sessionStorage.setItem(
      'agents.composer.draft',
      JSON.stringify({
        value: { task: 'Fix the flaky test', repo: '', runtime: '', model: '' },
        savedAt: Date.now(),
      }),
    );
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ repos: ['owner/other', 'owner/repository'] }),
    );
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);

    render(<TaskComposer />);

    // Open already, as Enter would have opened it; nothing was chosen for them.
    const dialog = await screen.findByRole('dialog', { name: 'Repository' });
    expect(within(dialog).getByRole('searchbox')).toHaveFocus();
    const box = screen.getByRole('textbox', { name: 'What should the agent do?' });
    expect(box).toHaveValue('Fix the flaky test');
    expect(createAgentJob).not.toHaveBeenCalled();

    // Choosing hands the caret back to the task, so the next key sends it.
    fireEvent.click(within(dialog).getByRole('button', { name: 'owner/repository' }));
    expect(box).toHaveFocus();
    await screen.findByLabelText('Branch');
    await untilRunnable();
    fireEvent.keyDown(box, { key: 'Enter' });
    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith(
        expect.objectContaining({ repo: 'owner/repository', task_prompt: 'Fix the flaky test' }),
      ),
    );
  });

  it('takes a trip to GitHub asked for elsewhere on the page, draft and all', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(disconnected());
    vi.mocked(githubAccessUrl).mockResolvedValue('https://github.test/login/oauth/authorize');

    render(<TaskComposer />);
    fireEvent.change(await screen.findByRole('textbox'), {
      target: { value: 'Fix the flaky test' },
    });
    // The sidebar's getting-started step asks; this box is the one that leaves.
    let taken = false;
    act(() => {
      taken = requestGitHubAccess('connect');
    });

    expect(taken).toBe(true);
    await waitFor(() =>
      expect(leaveFor).toHaveBeenCalledWith('https://github.test/login/oauth/authorize'),
    );
    // Connecting is for a repository: the temporary workspace is not kept.
    expect(JSON.parse(sessionStorage.getItem('agents.composer.draft')!).value).toMatchObject({
      task: 'Fix the flaky test',
      repo: '',
    });
  });

  // Where the App is configured, choosing a repository waits for the owner's
  // own connection, allowlisted ones included, though `/config` lists them.
  it('offers no repository before GitHub is connected, allowlisted ones included', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(disconnected({ repos: ['owner/allowlisted'] }));

    render(<TaskComposer />);

    expect(await repositoryButton()).toHaveAccessibleName('Repository: Start from scratch');
    fireEvent.click(await repositoryButton());
    const dialog = await screen.findByRole('dialog', { name: 'Repository' });
    expect(repositoryOptions(dialog)).toEqual([TEMPORARY_OPTION]);
    expect(
      within(dialog).getByRole('button', { name: 'Connect GitHub to use a repository' }),
    ).toBeInTheDocument();
  });

  // The app's first-run answer, as the layout provides it.
  function composerInApp() {
    return render(
      <FirstRunProvider userId="alice">
        <TaskComposer />
      </FirstRunProvider>,
    );
  }

  it('tells someone with no task yet what a task becomes', async () => {
    vi.mocked(listAgentConversations).mockResolvedValue({ conversations: [], next_cursor: null });
    vi.mocked(getAgentConfig).mockResolvedValue(config());

    composerInApp();

    expect(
      await screen.findByText(
        'Describe a task. The agent works in an isolated sandbox and opens a draft pull request in your repository.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText('Runs in an isolated sandbox.')).not.toBeInTheDocument();
  });

  it('tells a newcomer without a repository where the task runs', async () => {
    vi.mocked(listAgentConversations).mockResolvedValue({ conversations: [], next_cursor: null });
    vi.mocked(getAgentConfig).mockResolvedValue(disconnected());

    composerInApp();

    expect(
      await screen.findByText(
        'Describe a task. The agent works in an isolated sandbox that starts empty.',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/opens a draft pull request/)).not.toBeInTheDocument();
  });

  it('keeps the usual line for someone who has had a task, even an archived one', async () => {
    vi.mocked(listAgentConversations).mockResolvedValue({
      conversations: [conversation(true)],
      next_cursor: null,
    });
    vi.mocked(getAgentConfig).mockResolvedValue(config());

    composerInApp();

    await waitFor(() => expect(listAgentConversations).toHaveBeenCalled());
    expect(await screen.findByText('Runs in an isolated sandbox.')).toBeInTheDocument();
    expect(screen.queryByText(/opens a draft pull request/)).not.toBeInTheDocument();
  });

  it('asks nothing itself: the answer is the app’s', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());

    render(<TaskComposer />);

    expect(await screen.findByText('Runs in an isolated sandbox.')).toBeInTheDocument();
    expect(listAgentConversations).not.toHaveBeenCalled();
  });

  // Connected, and the App reaches no repository: the next step is repository
  // access, not connecting again. The task can run in a temporary workspace
  // meanwhile, and the trip to GitHub keeps what was typed.
  it('asks for repository access, not a second connection, when none came with it', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ repos: [], github_install_url: '/agents/integrations' }),
    );
    vi.mocked(githubAccessUrl).mockResolvedValue(
      'https://github.test/apps/agent/installations/new',
    );

    render(<TaskComposer />);

    expect(
      await screen.findByText('Give the GitHub App access to a repository'),
    ).toBeInTheDocument();
    expect(screen.getByText(/GitHub is connected/)).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: 'Connect GitHub to use a repository' }),
    ).not.toBeInTheDocument();
    expect(await repositoryButton()).toHaveAccessibleName('Repository: Start from scratch');
    const box = screen.getByRole('textbox');
    fireEvent.change(box, { target: { value: 'Tidy the README' } });
    await waitFor(() => expect(screen.getByRole('button', { name: /^Run/ })).toBeEnabled());

    fireEvent.click(screen.getByRole('button', { name: /Configure repository access/ }));
    await waitFor(() =>
      expect(leaveFor).toHaveBeenCalledWith('https://github.test/apps/agent/installations/new'),
    );
    expect(githubAccessUrl).toHaveBeenCalledWith('manage');
    expect(JSON.parse(sessionStorage.getItem('agents.composer.draft')!).value).toMatchObject({
      task: 'Tidy the README',
      repo: '',
    });
  });

  it.each(['onboarding', 'repository picker'] as const)(
    'creates a repository from %s while preserving the task draft',
    async (entry) => {
      vi.mocked(getAgentConfig)
        .mockResolvedValueOnce(
          config({ repos: entry === 'onboarding' ? [] : ['owner/repository'] }),
        )
        .mockResolvedValue(config({ repos: ['owner/repository', 'owner/new-repo'] }));
      vi.mocked(createGitHubRepository).mockResolvedValue({
        repo: 'owner/new-repo',
        web_url: 'https://github.com/owner/new-repo',
        default_branch: 'main',
        accessible: true,
      });
      render(<TaskComposer />);
      const task = await screen.findByRole('textbox');
      fireEvent.change(task, { target: { value: 'Build a small app' } });
      if (entry === 'repository picker') {
        fireEvent.click(await repositoryButton());
        const picker = await screen.findByRole('dialog', { name: 'Repository' });
        fireEvent.click(within(picker).getByRole('button', { name: 'Create repository' }));
      } else {
        fireEvent.click(screen.getByRole('button', { name: 'Create repository' }));
      }
      await screen.findByRole('textbox', { name: 'Repository name' });
      const dialog = screen.getByRole('dialog', { name: 'Create repository' });
      fireEvent.change(within(dialog).getByRole('textbox', { name: 'Repository name' }), {
        target: { value: 'new-repo' },
      });
      fireEvent.submit(dialog.querySelector('form')!);
      expect(
        await screen.findByRole('button', { name: 'Repository: owner/new-repo' }),
      ).toBeInTheDocument();
      expect(task).toHaveValue('Build a small app');
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
      expect(createGitHubRepository).toHaveBeenCalledExactlyOnceWith({
        name: 'new-repo',
        private: true,
      });
      expect(getAgentConfig).toHaveBeenLastCalledWith({ fresh: true });
      expect(createAgentJob).not.toHaveBeenCalled();
      expect(navigation.push).not.toHaveBeenCalled();
    },
  );

  it('puts the draft back after the trip to GitHub, and only once', async () => {
    sessionStorage.setItem(
      'agents.composer.draft',
      JSON.stringify({
        value: {
          task: 'Fix the flaky test',
          repo: 'owner/new',
          runtime: 'codex',
          model: 'model-b',
        },
        savedAt: Date.now(),
      }),
    );
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        repos: ['owner/new', 'owner/repository'],
        runtimes: ['claude-code', 'codex'],
        models: ['model-a', 'model-b'],
      }),
    );

    render(<TaskComposer />);

    expect(
      await screen.findByRole('button', { name: 'Repository: owner/new' }),
    ).toBeInTheDocument();
    expect(screen.getByRole('textbox')).toHaveValue('Fix the flaky test');
    expect(screen.getByLabelText('Agent')).toHaveValue('codex');
    expect(screen.getByLabelText('Model')).toHaveValue('model-b');
    expect(sessionStorage.getItem('agents.composer.draft')).toBeNull();
  });

  it('does not put back a draft from a trip long abandoned', async () => {
    sessionStorage.setItem(
      'agents.composer.draft',
      JSON.stringify({
        value: { task: 'Old idea', repo: 'owner/repository', runtime: '', model: '' },
        savedAt: Date.now() - 31 * 60 * 1000,
      }),
    );
    vi.mocked(getAgentConfig).mockResolvedValue(config());

    render(<TaskComposer />);

    expect(await repositoryButton()).toHaveAccessibleName('Repository: Select repository');
    expect(screen.getByRole('textbox')).toHaveValue('');
  });

  it('adds repositories on GitHub from the repository list', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ github_install_url: '/agents/integrations' }),
    );
    vi.mocked(githubAccessUrl).mockResolvedValue(
      'https://github.test/apps/agent/installations/new',
    );

    render(<TaskComposer />);
    fireEvent.click(await repositoryButton());
    const dialog = await screen.findByRole('dialog', { name: 'Repository' });

    fireEvent.click(within(dialog).getByRole('button', { name: 'Configure repository access' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await waitFor(() =>
      expect(leaveFor).toHaveBeenCalledWith('https://github.test/apps/agent/installations/new'),
    );
  });

  // A deployment without a GitHub App has nothing to connect: tasks run in
  // temporary workspaces, and nothing points at a setup nobody here can do.
  it('runs in a temporary workspace where the deployment has no GitHub App', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(disconnected({ github_install_url: null }));

    render(<TaskComposer />);

    expect(await repositoryButton()).toHaveAccessibleName('Repository: Start from scratch');
    expect(
      screen.queryByRole('button', { name: 'Connect GitHub to use a repository' }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    const box = screen.getByRole('textbox');
    expect(box).toBeEnabled();
    fireEvent.change(box, { target: { value: 'Plot the survey results' } });
    await waitFor(() => expect(screen.getByRole('button', { name: /^Run/ })).toBeEnabled());

    // The one place a task can run, and no repository to manage.
    fireEvent.click(await repositoryButton());
    const dialog = await screen.findByRole('dialog', { name: 'Repository' });
    expect(repositoryOptions(dialog)).toEqual([TEMPORARY_OPTION]);
    expect(within(dialog).queryByRole('link')).not.toBeInTheDocument();
    expect(
      within(dialog).queryByRole('button', { name: /GitHub|Refresh|Create/ }),
    ).not.toBeInTheDocument();
    expect(dialog).not.toHaveTextContent('No repositories yet.');
  });

  // Issue #47: the deployment's App failed to load and the service is retrying.
  // Nobody has anything to connect or configure, so the notice says to wait.
  // A temporary workspace waits too: the create endpoint answers 503 for every
  // task until the App is back.
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
    expect(
      screen.queryByRole('button', { name: 'Connect GitHub to use a repository' }),
    ).not.toBeInTheDocument();
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

  // The list is empty during the outage because GitHub is down, not because
  // the repository is gone, so the project link still means what it said.
  it('keeps a project link’s repository through a GitHub outage', async () => {
    vi.useFakeTimers();
    try {
      vi.mocked(getAgentConfig)
        .mockResolvedValueOnce(
          config({
            repos: [],
            github_connected: false,
            github_install_url: null,
            github_load_status: 'unavailable',
          }),
        )
        .mockResolvedValue(
          config({ repos: ['owner/repository', 'owner/requested'], github_load_status: 'ready' }),
        );
      await act(async () => {
        render(<TaskComposer initialRepo="owner/requested" />);
      });
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      expect(
        screen.getByRole('button', { name: 'Repository: owner/requested' }),
      ).toBeInTheDocument();
      expect(screen.queryByText(/is not available to you/)).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps the connected composer interactive and submits its selected values', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);

    render(<TaskComposer />);

    // Nothing is chosen for the owner, and nothing runs until they choose.
    expect(await repositoryButton()).toHaveAccessibleName('Repository: Select repository');
    expect(screen.queryByLabelText('Branch')).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Fix the flaky test' } });
    expect(screen.getByRole('button', { name: /Run/ })).toBeDisabled();
    expect(listRepoBranches).not.toHaveBeenCalled();

    await chooseRepository();
    expect(await repositoryButton()).toHaveAccessibleName('Repository: owner/repository');
    expect(screen.getByLabelText('Branch')).toHaveValue('dev');
    expect(screen.getByLabelText('Agent')).toHaveValue('claude-code');
    expect(screen.getByLabelText('Model')).toHaveValue('model-a');
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByRole('textbox')).not.toBeDisabled();

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Fix the flaky test' } });
    await runWhenReady();

    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith({
        repo: 'owner/repository',
        task_prompt: 'Fix the flaky test',
        runtime: 'claude-code',
        model: 'model-a',
        base_ref: 'dev',
        environment_id: null,
        environment_revision: 0,
        global_revision: 0,
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
    await chooseRepository();
    const skills = await skillsMenuItem();
    expect(skills).toHaveTextContent('1');
    fireEvent.click(skills);
    fireEvent.click(await screen.findByRole('switch'));
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }));
    expect(await skillsMenuItem()).toHaveTextContent('0');
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Review the diff' } });
    await runWhenReady();
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
    await chooseRepository();
    expect(await skillsMenuItem()).toHaveTextContent('1');
    fireEvent.keyDown(screen.getByRole('menu'), { key: 'Escape' });
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Review the diff' } });
    await runWhenReady();
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
      await chooseRepository();
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
    await chooseRepository();
    await untilIdle();
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
    await chooseRepository();
    await pick(view.container, [new File(['hello world!'], 'notes.md')]);

    expect(await screen.findByText('notes.md')).toBeInTheDocument();
    // In the composer body, with the text — not among the run settings.
    expect(screen.getByRole('textbox').closest('div')).toContainElement(
      screen.getByText('notes.md'),
    );

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Read my notes' } });
    await runWhenReady();
    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith(
        expect.objectContaining({ attachment_ids: [41] }),
      ),
    );
  });

  it('pastes images synchronously and sends uploads with unique names across repeated pastes and remounts', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    let attachmentId = 41;
    vi.mocked(uploadAttachment).mockImplementation(async (file, path) => ({
      id: attachmentId++,
      path: path!,
      size_bytes: file.size,
      content_type: file.type,
    }));
    const view = render(<TaskComposer />);
    await chooseRepository();
    const box = screen.getByRole('textbox');
    fireEvent.change(box, { target: { value: 'Compare these screenshots' } });
    const image = new File(['pixels'], 'image.png', { type: 'image/png' });
    const textFile = new File(['notes'], 'notes.txt', { type: 'text/plain' });
    const pasteImage = (target: HTMLElement) => {
      const getAsFile = vi.fn<() => File | null>(() => image);
      const event = createEvent.paste(target, {
        clipboardData: {
          items: [
            { kind: 'string', type: 'text/plain', getAsFile: () => null },
            { kind: 'file', type: 'image/png', getAsFile },
            { kind: 'file', type: 'text/plain', getAsFile: () => textFile },
          ],
          // Browsers expose the same image here too; it must be uploaded once.
          files: [image, textFile],
        },
      });
      fireEvent(target, event);
      expect(getAsFile).toHaveBeenCalledOnce();
      expect(event.defaultPrevented).toBe(true);
      // Clipboard file access expires when the synchronous paste handler exits.
      getAsFile.mockReturnValue(null);
    };

    await untilIdle();
    pasteImage(box);
    pasteImage(box);
    await waitFor(() => expect(screen.getByRole('button', { name: /Run/ })).toBeEnabled());
    expect(uploadAttachment).toHaveBeenCalledTimes(2);
    expect(box).toHaveValue('Compare these screenshots');
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));
    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith(
        expect.objectContaining({ attachment_ids: [41, 42] }),
      ),
    );

    // Attachments from later messages share the archive with earlier uploads.
    view.unmount();
    render(<TaskComposer />);
    await chooseRepository();
    await untilIdle();
    pasteImage(screen.getByRole('textbox'));
    await waitFor(() => expect(uploadAttachment).toHaveBeenCalledTimes(3));
    const paths = vi.mocked(uploadAttachment).mock.calls.map(([file, path]) => {
      expect(file).toEqual(
        expect.objectContaining({ name: path, type: 'image/png', size: image.size }),
      );
      expect(path).toMatch(/^pasted-image-[a-f0-9-]+\.png$/);
      return path;
    });
    expect(new Set(paths).size).toBe(3);
  });

  it.each(['plain text', 'non-image file'])(
    'leaves a %s paste to the browser without uploading it',
    async (kind) => {
      vi.mocked(getAgentConfig).mockResolvedValue(config());
      render(<TaskComposer />);
      await chooseRepository();
      const box = screen.getByRole('textbox');
      fireEvent.change(box, { target: { value: 'Existing task' } });
      const file = new File(['notes'], 'notes.txt', { type: 'text/plain' });
      const event = createEvent.paste(box, {
        clipboardData: {
          items:
            kind === 'plain text'
              ? [{ kind: 'string', type: 'text/plain', getAsFile: () => null }]
              : [{ kind: 'file', type: 'text/plain', getAsFile: () => file }],
          files: kind === 'plain text' ? [] : [file],
          getData: () => 'Pasted text',
        },
      });
      fireEvent(box, event);
      expect(event.defaultPrevented).toBe(false);
      expect(uploadAttachment).not.toHaveBeenCalled();
      expect(box).toHaveValue('Existing task');
    },
  );

  it('takes pasted images for a temporary workspace before GitHub is connected', async () => {
    account.id = 'alice';
    localStorage.setItem(ACKNOWLEDGED, '1');
    vi.mocked(getAgentConfig).mockResolvedValue(disconnected());
    vi.mocked(uploadAttachment).mockImplementation(async (file, path) => ({
      id: 7,
      path: path!,
      size_bytes: file.size,
      content_type: file.type,
    }));
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    render(<TaskComposer />);
    const box = await screen.findByRole('textbox', { name: 'What should the agent do?' });
    fireEvent.change(box, { target: { value: 'Describe this chart' } });

    await untilIdle();
    fireEvent.paste(box, {
      clipboardData: {
        items: [],
        files: [new File(['pixels'], 'image.png', { type: 'image/png' })],
      },
    });

    await waitFor(() => expect(uploadAttachment).toHaveBeenCalledOnce());
    await runWhenReady();
    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith(
        expect.objectContaining({ repo: null, attachment_ids: [7] }),
      ),
    );
  });

  it('does not upload pasted images while GitHub is unavailable', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      disconnected({ github_install_url: null, github_load_status: 'unavailable' }),
    );
    render(<TaskComposer />);
    await screen.findByText('GitHub is temporarily unavailable');
    fireEvent.paste(screen.getByRole('textbox'), {
      clipboardData: {
        items: [],
        files: [new File(['pixels'], 'image.png', { type: 'image/png' })],
      },
    });
    expect(uploadAttachment).not.toHaveBeenCalled();
  });

  it('does not upload pasted images while a task is being created', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockReturnValue(new Promise(() => {}));
    render(<TaskComposer />);
    await chooseRepository();
    const box = screen.getByRole('textbox');
    fireEvent.change(box, { target: { value: 'Start the task' } });
    await runWhenReady();
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledOnce());
    fireEvent.paste(box, {
      clipboardData: {
        items: [],
        files: [new File(['pixels'], 'image.png', { type: 'image/png' })],
      },
    });
    expect(uploadAttachment).not.toHaveBeenCalled();
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
    await chooseRepository();
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
    await chooseRepository();
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
    await chooseRepository();
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
    await chooseRepository();
    await pick(view.container, [new File(['hello world!'], 'notes.md')]);
    fireEvent.click(await screen.findByRole('button', { name: 'Remove notes.md' }));

    await waitFor(() => expect(deleteAttachment).toHaveBeenCalledWith(41));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Read my notes' } });
    await runWhenReady();
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

  it('will not start a task while a dropped folder is still being read', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    vi.mocked(uploadAttachment).mockImplementation(async (_file, path) => ({
      id: 3,
      path: path!,
      size_bytes: 4,
      content_type: 'text/plain',
    }));
    render(<TaskComposer />);
    await chooseRepository();
    const textbox = screen.getByRole('textbox');
    fireEvent.change(textbox, { target: { value: 'Read my notes' } });

    // A folder is walked one file at a time, and this file has not been read.
    let readFile: () => void = () => {};
    const slow = {
      ...droppedFile('/specs/api.md'),
      file: (ok: (file: File) => void) => {
        readFile = () => ok(new File(['data'], 'api.md'));
      },
    };
    const entry = droppedFolder('/specs', slow);
    await act(async () => {
      fireEvent.drop(textbox.closest('div')!, {
        dataTransfer: {
          types: ['Files'],
          items: [{ kind: 'file', webkitGetAsEntry: () => entry }],
          files: [],
        },
      });
    });

    // Running now would create the task without the folder being dropped.
    const run = screen.getByRole('button', { name: /Run/ });
    expect(run).toBeDisabled();
    fireEvent.keyDown(textbox, { key: 'Enter' });
    await act(async () => {});
    expect(createAgentJob).not.toHaveBeenCalled();

    await act(async () => readFile());
    expect(await screen.findByText('specs/')).toBeInTheDocument();
    await waitFor(() => expect(run).toBeEnabled());
    fireEvent.click(run);
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).toMatchObject({ attachment_ids: [3] });
  });

  it('waits for every dropped folder, not just the first one read', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(uploadAttachment).mockImplementation(async (_file, path) => ({
      id: 3,
      path: path!,
      size_bytes: 4,
      content_type: 'text/plain',
    }));
    render(<TaskComposer />);
    await chooseRepository();
    const textbox = screen.getByRole('textbox');
    fireEvent.change(textbox, { target: { value: 'Read my notes' } });
    const reads: Array<() => void> = [];
    for (const folder of ['/specs', '/notes']) {
      const slow = {
        ...droppedFile(`${folder}/readme.md`),
        file: (ok: (file: File) => void) => {
          reads.push(() => ok(new File(['data'], 'readme.md')));
        },
      };
      const entry = droppedFolder(folder, slow);
      await act(async () => {
        fireEvent.drop(textbox.closest('div')!, {
          dataTransfer: {
            types: ['Files'],
            items: [{ kind: 'file', webkitGetAsEntry: () => entry }],
            files: [],
          },
        });
      });
    }

    const run = screen.getByRole('button', { name: /Run/ });
    await act(async () => reads[0]());
    expect(await screen.findByText('specs/')).toBeInTheDocument();
    expect(run).toBeDisabled();
    await act(async () => reads[1]());
    expect(await screen.findByText('notes/')).toBeInTheDocument();
    await waitFor(() => expect(run).toBeEnabled());
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
    await chooseRepository();
    await openMcpSubmenu();
    fireEvent.click(await screen.findByRole('switch', { name: 'Notion' }));

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Sync the spec' } });
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Notion' })).toBeChecked());
    await runWhenReady();

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
    await chooseRepository();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Sync the spec' } });
    await runWhenReady();
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
    await chooseRepository();
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
    await chooseRepository();
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
    await chooseRepository();
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

    expect(
      await screen.findByRole('button', { name: 'Repository: owner/requested' }),
    ).toBeInTheDocument();
    await waitFor(() => expect(listRepoBranches).toHaveBeenCalledWith('owner/requested'));
  });

  // It used to fall back to the first repository in the list: a task meant for
  // one repository, sent to another without a word.
  it('says a requested repository is unavailable instead of choosing another one', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ repos: ['owner/default', 'owner/another'] }),
    );

    render(<TaskComposer initialRepo="owner/deleted" />);

    expect(await screen.findByRole('status')).toHaveTextContent(
      'owner/deleted is not available to you',
    );
    expect(await repositoryButton()).toHaveAccessibleName('Repository: Select repository');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Fix it' } });
    expect(screen.getByRole('button', { name: /Run/ })).toBeDisabled();
    expect(listRepoBranches).not.toHaveBeenCalled();

    await chooseRepository('owner/another');
    expect(screen.queryByText(/owner\/deleted/)).not.toBeInTheDocument();
  });

  it('asks where to run on Enter, then sends once a repository is chosen', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);

    render(<TaskComposer />);
    const box = await screen.findByRole('textbox');
    fireEvent.change(box, { target: { value: 'Fix the flaky test' } });
    fireEvent.keyDown(box, { key: 'Enter' });

    // The key opened the question instead of sending, or being ignored.
    const dialog = await screen.findByRole('dialog', { name: 'Repository' });
    expect(within(dialog).getByRole('searchbox')).toHaveFocus();
    expect(createAgentJob).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole('button', { name: 'owner/repository' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    // The caret is back in the task, so the same key now sends it.
    expect(box).toHaveFocus();
    await screen.findByLabelText('Branch');
    await untilRunnable();
    fireEvent.keyDown(box, { key: 'Enter' });

    await waitFor(() =>
      expect(createAgentJob).toHaveBeenCalledWith(
        expect.objectContaining({ repo: 'owner/repository', base_ref: 'dev' }),
      ),
    );
  });

  it('finds a repository by part of its name, and takes the first match on Enter', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ repos: ['Zeta/tools', 'alice/site', 'HarvardMadSys/hybridInference'] }),
    );

    render(<TaskComposer />);
    fireEvent.click(await repositoryButton());
    const dialog = await screen.findByRole('dialog', { name: 'Repository' });
    // Sorted without regard to case, so one owner's capitals do not float it
    // above everyone else's; the temporary workspace is not a repository and
    // heads the list.
    expect(repositoryOptions(dialog)).toEqual([
      TEMPORARY_OPTION,
      'alice/site',
      'HarvardMadSys/hybridInference',
      'Zeta/tools',
    ]);

    const search = within(dialog).getByRole('searchbox');
    fireEvent.change(search, { target: { value: 'nobody' } });
    expect(dialog).toHaveTextContent('Nothing matches “nobody”.');
    expect(
      within(dialog).getByRole('link', { name: 'Configure repository access' }),
    ).toHaveAttribute('href', '/settings/integrations');
    expect(within(dialog).getByRole('link', { name: 'Create new repository' })).toHaveAttribute(
      'href',
      'https://github.com/new',
    );
    expect(within(dialog).getByRole('link', { name: 'Create new repository' })).toHaveAttribute(
      'target',
      '_blank',
    );
    // Enter with nothing to take chooses nothing.
    fireEvent.keyDown(search, { key: 'Enter' });
    expect(screen.getByRole('dialog', { name: 'Repository' })).toBeInTheDocument();

    fireEvent.change(search, { target: { value: 'hybrid' } });
    fireEvent.keyDown(search, { key: 'Enter' });
    expect(
      await screen.findByRole('button', { name: 'Repository: HarvardMadSys/hybridInference' }),
    ).toHaveFocus();
  });

  // "(no repository)" would answer every search for a repository, so the
  // temporary workspace is found by its name alone, and after the matches.
  it('finds the temporary workspace by its name, after any repository that matches', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ repos: ['owner/repository', 'owner/scratchpad'] }),
    );

    render(<TaskComposer />);
    fireEvent.click(await repositoryButton());
    const dialog = await screen.findByRole('dialog', { name: 'Repository' });
    const search = within(dialog).getByRole('searchbox');

    fireEvent.change(search, { target: { value: 'repo' } });
    expect(repositoryOptions(dialog)).toEqual(['owner/repository']);
    fireEvent.change(search, { target: { value: 'scratch' } });
    expect(repositoryOptions(dialog)).toEqual(['owner/scratchpad', TEMPORARY_OPTION]);
    fireEvent.change(search, { target: { value: 'start' } });
    expect(repositoryOptions(dialog)).toEqual([TEMPORARY_OPTION]);
    expect(dialog).not.toHaveTextContent('Nothing matches');

    fireEvent.keyDown(search, { key: 'Enter' });
    expect(
      await screen.findByRole('button', { name: 'Repository: Start from scratch' }),
    ).toHaveFocus();
    expect(screen.queryByLabelText('Branch')).not.toBeInTheDocument();
  });

  it('runs a connected owner’s task in a temporary workspace chosen from the list', async () => {
    account.id = 'alice';
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);

    render(<TaskComposer />);
    // Connected: nothing is chosen for the owner, the temporary workspace included.
    expect(await repositoryButton()).toHaveAccessibleName('Repository: Select repository');
    await chooseRepository();
    fireEvent.click(await repositoryButton());
    fireEvent.click(
      within(await screen.findByRole('dialog', { name: 'Repository' })).getByRole('button', {
        name: TEMPORARY_OPTION,
      }),
    );

    expect(await repositoryButton()).toHaveAccessibleName('Repository: Start from scratch');
    // The repository's branch goes with the repository.
    expect(screen.queryByLabelText('Branch')).not.toBeInTheDocument();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Try an idea' } });
    await runWhenReady();
    fireEvent.click(
      within(await screen.findByRole('alertdialog', { name: 'Before you start' })).getByRole(
        'button',
        { name: 'Got it' },
      ),
    );

    await waitFor(() => expect(createAgentJob).toHaveBeenCalledOnce());
    const request = vi.mocked(createAgentJob).mock.calls[0][0];
    expect(request).toMatchObject({ repo: null, environment_id: null, global_revision: 3 });
    expect(request).not.toHaveProperty('base_ref');
  });

  it('starts in a temporary workspace when the link asks for one', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());

    render(<TaskComposer initialRepo=":temporary" />);

    expect(await repositoryButton()).toHaveAccessibleName('Repository: Start from scratch');
    expect(screen.queryByText(/is not available to you/)).not.toBeInTheDocument();
  });

  it('closes on Escape without choosing anything', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());

    render(<TaskComposer />);
    const trigger = await repositoryButton();
    fireEvent.click(trigger);
    fireEvent.keyDown(await screen.findByRole('searchbox'), { key: 'Escape' });

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAccessibleName('Repository: Select repository');
  });

  it('reads the list again on Refresh and keeps the repository already chosen', async () => {
    vi.mocked(getAgentConfig)
      .mockResolvedValueOnce(config({ repos: ['owner/repository'] }))
      .mockResolvedValue(config({ repos: ['owner/new', 'owner/repository'] }));

    render(<TaskComposer />);
    await chooseRepository();
    fireEvent.click(await repositoryButton());
    const dialog = await screen.findByRole('dialog', { name: 'Repository' });
    await act(async () => {
      fireEvent.click(within(dialog).getByRole('button', { name: 'Refresh' }));
    });

    expect(getAgentConfig).toHaveBeenCalledTimes(2);
    // Asking GitHub again, not the service's minute-old answer.
    expect(getAgentConfig).toHaveBeenLastCalledWith({ fresh: true });
    expect(await within(dialog).findByRole('button', { name: 'owner/new' })).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: 'Repository: owner/repository' }),
    ).toBeInTheDocument();
  });

  it('chooses a requested repository that a refresh makes available', async () => {
    vi.mocked(getAgentConfig)
      .mockResolvedValueOnce(config({ repos: ['owner/repository'] }))
      .mockResolvedValue(config({ repos: ['owner/repository', 'owner/requested'] }));

    render(<TaskComposer initialRepo="owner/requested" />);
    expect(await screen.findByRole('status')).toHaveTextContent('owner/requested');
    fireEvent.click(await repositoryButton());
    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Refresh' }));
    });

    expect(
      await screen.findByRole('button', { name: 'Repository: owner/requested' }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('starts a plain new task empty after a project link chose a repository', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ repos: ['owner/repository', 'owner/requested'] }),
    );

    const view = render(<TaskComposer initialRepo="owner/requested" />);
    await screen.findByRole('button', { name: 'Repository: owner/requested' });

    // "New task" in the sidebar is the same page without `?repo=`, so React
    // keeps this composer and only the request changes.
    view.rerender(<TaskComposer />);

    expect(
      await screen.findByRole('button', { name: 'Repository: Select repository' }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText('Branch')).not.toBeInTheDocument();
  });

  it("never sends one repository's branch with another", async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config({ repos: ['owner/a', 'owner/b'] }));
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);
    vi.mocked(listRepoBranches)
      .mockResolvedValueOnce({ default: 'release', branches: ['release'] })
      .mockReturnValueOnce(new Promise(() => {}));

    render(<TaskComposer />);
    await chooseRepository('owner/a');
    expect(screen.getByLabelText('Branch')).toHaveValue('release');

    fireEvent.click(await repositoryButton());
    fireEvent.click(
      within(await screen.findByRole('dialog', { name: 'Repository' })).getByRole('button', {
        name: 'owner/b',
      }),
    );
    await waitFor(() => expect(screen.queryByLabelText('Branch')).not.toBeInTheDocument());

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Ship it' } });
    await runWhenReady();
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    // No `base_ref`: the repository's own default, not owner/a's `release`.
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).toMatchObject({ repo: 'owner/b' });
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).not.toHaveProperty('base_ref');
  });

  it('runs the task on Enter and keeps Shift+Enter a newline', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);

    render(<TaskComposer />);
    await chooseRepository();

    const composer = screen.getByRole('textbox');
    fireEvent.change(composer, { target: { value: 'Fix the flaky test' } });
    await untilRunnable();

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
        environment_id: null,
        environment_revision: 0,
        global_revision: 0,
      }),
    );
    expect(navigation.push).toHaveBeenCalledWith('/ajob_1');
  });

  it('leaves Enter to the IME while a candidate is being composed', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'ajob_1' } as never);

    render(<TaskComposer />);
    await chooseRepository();

    const composer = screen.getByRole('textbox');
    fireEvent.change(composer, { target: { value: '\u4fee\u590d' } });
    await untilRunnable();

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
    await chooseRepository();

    const composer = screen.getByRole('textbox');
    fireEvent.change(composer, { target: { value: 'Fix the flaky test' } });
    await untilRunnable();
    fireEvent.keyDown(composer, { key: 'Enter', ...modifier });

    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    expect(navigation.push).toHaveBeenCalledWith('/ajob_1');
  }

  it('sends the task on \u2318+Enter too', () => expectChordSends({ metaKey: true }));

  it('sends the task on Ctrl+Enter too', () => expectChordSends({ ctrlKey: true }));

  it('swallows Enter on an empty task rather than growing a blank line', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());

    render(<TaskComposer />);
    await chooseRepository();

    const composer = screen.getByRole('textbox');
    fireEvent.change(composer, { target: { value: '   ' } });

    // Enter is the send key on this composer and never the newline, so with
    // nothing to send it does nothing at all. Shift+Enter still breaks the line.
    expect(fireEvent.keyDown(composer, { key: 'Enter' })).toBe(false);
    expect(fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true })).toBe(true);
    expect(createAgentJob).not.toHaveBeenCalled();
  });
});

describe('durable new-task drafts', () => {
  beforeEach(() => {
    account.id = 'alice';
    sessionStorage.clear();
    navigation.push.mockReset();
    vi.mocked(getAgentConfig).mockReset().mockResolvedValue(config());
    vi.mocked(listRepoBranches)
      .mockReset()
      .mockResolvedValue({ default: 'dev', branches: ['dev', 'main'] });
    vi.mocked(listSkills).mockResolvedValue([]);
    vi.mocked(listMcpConnections).mockResolvedValue({ connections: [], templates: [] });
    vi.mocked(getMcpConnectionCapabilities).mockResolvedValue([]);
    vi.mocked(createAgentJob).mockReset();
  });
  afterEach(cleanup);

  it('restores exact unsent text and validated repository and branch choices after navigation', async () => {
    const first = render(<TaskComposer />);
    await chooseRepository();
    fireEvent.change(screen.getByRole('textbox'), {
      target: { value: '  Review this change\ncarefully.  ' },
    });
    fireEvent.click(screen.getByLabelText('Branch'));
    fireEvent.click(screen.getByRole('option', { name: 'main' }));
    first.unmount();
    render(<TaskComposer />);
    expect(await screen.findByRole('textbox')).toHaveValue('  Review this change\ncarefully.  ');
    await waitFor(() => expect(screen.getByLabelText('Branch')).toHaveValue('main'));
    expect(await repositoryButton()).toHaveAccessibleName('Repository: owner/repository');
  });

  it('isolates accounts immediately and restores each account on return', async () => {
    const view = render(<TaskComposer />);
    fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'Alice draft' } });
    account.id = 'bob';
    view.rerender(<TaskComposer />);
    expect(await screen.findByRole('textbox')).toHaveValue('');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Bob draft' } });
    account.id = 'alice';
    view.rerender(<TaskComposer />);
    expect(await screen.findByRole('textbox')).toHaveValue('Alice draft');
  });

  it('preserves an unsent branch while its validation read is still pending', async () => {
    const first = render(<TaskComposer />);
    await chooseRepository();
    fireEvent.click(screen.getByLabelText('Branch'));
    fireEvent.click(screen.getByRole('option', { name: 'main' }));
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Original draft' } });
    first.unmount();
    vi.mocked(listRepoBranches).mockImplementationOnce(() => new Promise(() => undefined));
    const second = render(<TaskComposer />);
    fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'Updated draft' } });
    await waitFor(() => expect(listRepoBranches).toHaveBeenCalledTimes(2));
    second.unmount();
    render(<TaskComposer />);
    expect(await screen.findByRole('textbox')).toHaveValue('Updated draft');
    await waitFor(() => expect(screen.getByLabelText('Branch')).toHaveValue('main'));
  });

  it('does not navigate another account after an earlier account submission resolves', async () => {
    let finish!: (job: AgentJobApi) => void;
    vi.mocked(createAgentJob).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const view = render(<TaskComposer />);
    await chooseRepository();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Alice task' } });
    await runWhenReady();
    await waitFor(() => expect(finish).toBeDefined());
    account.id = 'bob';
    view.rerender(<TaskComposer />);
    fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'Bob draft' } });
    await act(async () => finish({ id: 'alice-task' } as AgentJobApi));
    expect(navigation.push).not.toHaveBeenCalled();
    expect(screen.getByRole('textbox')).toHaveValue('Bob draft');
    account.id = 'alice';
    view.rerender(<TaskComposer />);
    expect(await screen.findByRole('textbox')).toHaveValue('');
  });

  it('clears an accepted draft while retaining a draft edited during an in-flight request', async () => {
    let finish!: (job: AgentJobApi) => void;
    vi.mocked(createAgentJob).mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const first = render(<TaskComposer />);
    await chooseRepository();
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'First task' } });
    await runWhenReady();
    await waitFor(() => expect(finish).toBeDefined());
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Next task' } });
    await act(async () => finish({ id: 'accepted-task' } as AgentJobApi));
    first.unmount();
    const second = render(<TaskComposer />);
    expect(await screen.findByRole('textbox')).toHaveValue('Next task');
    vi.mocked(createAgentJob).mockResolvedValue({ id: 'next-task' } as AgentJobApi);
    await waitFor(() => expect(screen.getByRole('button', { name: /Run/ })).toBeEnabled());
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));
    await waitFor(() => expect(navigation.push).toHaveBeenCalledWith('/next-task'));
    second.unmount();
    render(<TaskComposer />);
    expect(await screen.findByRole('textbox')).toHaveValue('');
  });

  it('retries a failed initial configuration read without losing the draft', async () => {
    vi.mocked(getAgentConfig).mockRejectedValueOnce(new Error('Unavailable'));
    render(<TaskComposer />);
    fireEvent.click(await screen.findByRole('button', { name: 'Retry' }));
    await screen.findByRole('textbox');
    await waitFor(() => expect(screen.queryByText('Unavailable')).not.toBeInTheDocument());
    expect(getAgentConfig).toHaveBeenCalledWith({ fresh: true });
  });
});

describe('TaskComposer plugins', () => {
  const plugin = {
    plugin: 'code-review@claude-plugins-official',
    marketplace: 'anthropics/claude-plugins-official',
  };
  beforeEach(() => {
    account.id = 'alice';
    sessionStorage.clear();
    navigation.push.mockReset();
    vi.mocked(getAgentConfig)
      .mockReset()
      .mockResolvedValue(
        config({ runtimes: ['claude-code', 'pi'], plugin_runtimes: ['claude-code'] }),
      );
    vi.mocked(listRepoBranches)
      .mockReset()
      .mockResolvedValue({ default: 'dev', branches: ['dev'] });
    vi.mocked(listSkills).mockResolvedValue([]);
    vi.mocked(listMcpConnections).mockResolvedValue({ connections: [], templates: [] });
    vi.mocked(getMcpConnectionCapabilities).mockResolvedValue([]);
    vi.mocked(createAgentJob)
      .mockReset()
      .mockResolvedValue({ id: 'plugin-task' } as AgentJobApi);
  });
  afterEach(cleanup);

  async function openPlugins() {
    // `initialRepo` is applied a render after the textbox appears, and the
    // composer is busy again while that repository's environments load.
    await screen.findByLabelText('Branch');
    const menu = await openAttachMenu();
    fireEvent.click(within(menu).getByRole('menuitem', { name: /Plugins/ }));
    await screen.findByRole('button', { name: 'Done' });
    return screen.getByRole('dialog', { name: 'Plugins' });
  }

  async function selectPlugin() {
    const dialog = within(await openPlugins());
    fireEvent.change(dialog.getByLabelText('Plugin'), { target: { value: plugin.plugin } });
    fireEvent.change(dialog.getByLabelText(/Marketplace source/), {
      target: { value: plugin.marketplace },
    });
    fireEvent.click(dialog.getByRole('button', { name: 'Add plugin' }));
    fireEvent.click(dialog.getByRole('button', { name: 'Done' }));
  }

  it.each(['Review the change', '/goal Review the change'])(
    'sends selected plugins with %s',
    async (prompt) => {
      render(<TaskComposer initialRepo="owner/repository" />);
      const box = await screen.findByRole('textbox', { name: 'What should the agent do?' });
      await selectPlugin();
      fireEvent.change(box, { target: { value: prompt } });
      await runWhenReady();
      await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
      expect(vi.mocked(createAgentJob).mock.calls[0][0]).toMatchObject({ plugins: [plugin] });
      expect(vi.mocked(createAgentJob).mock.calls[0][0].goal).toEqual(
        prompt.startsWith('/goal') ? { objective: 'Review the change' } : undefined,
      );
    },
  );

  it('omits plugins when none are selected', async () => {
    render(<TaskComposer initialRepo="owner/repository" />);
    fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'Review' } });
    await runWhenReady();
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).not.toHaveProperty('plugins');
  });

  it.each(['pi', 'opencode', 'kilo'])(
    'submits npm packages for %s without a marketplace',
    async (runtime) => {
      vi.mocked(getAgentConfig).mockResolvedValue(
        config({ runtimes: [runtime], plugin_runtimes: [runtime] }),
      );
      render(<TaskComposer initialRepo="owner/repository" />);
      const box = await screen.findByRole('textbox');
      const dialog = within(await openPlugins());
      fireEvent.change(dialog.getByLabelText(/^npm package/), {
        target: { value: 'npm:@scope/plugin@1.2.3' },
      });
      fireEvent.click(dialog.getByRole('button', { name: 'Add plugin' }));
      fireEvent.click(dialog.getByRole('button', { name: 'Done' }));
      fireEvent.change(box, { target: { value: 'Review' } });
      await runWhenReady();
      await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
      expect(vi.mocked(createAgentJob).mock.calls[0][0]).toMatchObject({
        runtime,
        plugins: [{ plugin: '@scope/plugin@1.2.3' }],
      });
    },
  );

  it('retains npm package selections when reopening the composer', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ runtimes: ['pi'], plugin_runtimes: ['pi'] }),
    );
    const first = render(<TaskComposer initialRepo="owner/repository" />);
    await screen.findByRole('textbox');
    const dialog = within(await openPlugins());
    fireEvent.change(dialog.getByLabelText(/^npm package/), {
      target: { value: '@scope/plugin@1.2.3' },
    });
    fireEvent.click(dialog.getByRole('button', { name: 'Add plugin' }));
    fireEvent.click(dialog.getByRole('button', { name: 'Done' }));
    first.unmount();
    render(<TaskComposer initialRepo="owner/repository" />);
    expect(within(await openPlugins()).getByText('@scope/plugin@1.2.3')).toBeInTheDocument();
  });

  it('blocks incompatible plugin formats when switching between supported agents', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ runtimes: ['claude-code', 'pi'], plugin_runtimes: ['claude-code', 'pi'] }),
    );
    render(<TaskComposer initialRepo="owner/repository" />);
    const box = await screen.findByRole('textbox');
    await selectPlugin();
    fireEvent.change(box, { target: { value: 'Review' } });
    fireEvent.click(screen.getByLabelText('Agent'));
    fireEvent.click(screen.getByRole('option', { name: 'pi' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Selected plugins do not match pi');
    expect(screen.getByRole('button', { name: /Run/ })).toBeDisabled();
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(createAgentJob).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText('Agent'));
    fireEvent.click(screen.getByRole('option', { name: 'claude-code' }));
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    await runWhenReady();
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).toMatchObject({ plugins: [plugin] });
  });

  it('restores selections after navigation and isolates accounts', async () => {
    const first = render(<TaskComposer initialRepo="owner/repository" />);
    await screen.findByRole('textbox');
    await selectPlugin();
    first.unmount();
    const second = render(<TaskComposer initialRepo="owner/repository" />);
    expect(within(await openPlugins()).getByText(plugin.plugin)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    account.id = 'bob';
    second.rerender(<TaskComposer initialRepo="owner/repository" />);
    expect(within(await openPlugins()).getByText('No plugins selected.')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Done' }));
    account.id = 'alice';
    second.rerender(<TaskComposer initialRepo="owner/repository" />);
    expect(within(await openPlugins()).getByText(plugin.plugin)).toBeInTheDocument();
  });

  it('retains incompatible selections and blocks submission until they are removed', async () => {
    render(<TaskComposer initialRepo="owner/repository" />);
    const box = await screen.findByRole('textbox');
    await selectPlugin();
    fireEvent.change(box, { target: { value: '/goal Review the change' } });
    fireEvent.click(screen.getByLabelText('Agent'));
    fireEvent.click(screen.getByRole('option', { name: 'pi' }));
    expect(screen.getByRole('alert')).toHaveTextContent('pi does not support plugins');
    expect(screen.getByRole('button', { name: /Run/ })).toBeDisabled();
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(createAgentJob).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Manage plugins' }));
    await screen.findByRole('button', { name: 'Done' });
    const dialog = within(screen.getByRole('dialog', { name: 'Plugins' }));
    expect(dialog.getByText(plugin.plugin)).toBeInTheDocument();
    fireEvent.click(dialog.getByRole('button', { name: `Remove ${plugin.plugin}` }));
    fireEvent.click(dialog.getByRole('button', { name: 'Done' }));
    expect(screen.getByRole('button', { name: /Run/ })).toBeEnabled();
    fireEvent.click(screen.getByRole('button', { name: /Run/ }));
    await waitFor(() => expect(createAgentJob).toHaveBeenCalledTimes(1));
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).not.toHaveProperty('plugins');
  });

  it('hides plugin setup when the control plane advertises no support', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    render(<TaskComposer />);
    expect(
      within(await openAttachMenu()).queryByRole('menuitem', { name: /Plugins/ }),
    ).not.toBeInTheDocument();
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
    fireEvent.click(picker);
    const offered = screen.getAllByRole('option').map((o) => o.textContent);
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
    fireEvent.click(picker);
    const offered = screen.getAllByRole('option').map((o) => o.textContent);
    expect(offered).toEqual(['low', 'medium', 'high']);
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
    await chooseRepository();
    const textarea = await screen.findByRole('textbox');
    fireEvent.change(textarea, { target: { value: 'do it' } });
    if (effort) {
      fireEvent.click(screen.getByLabelText('Reasoning'));
      fireEvent.click(screen.getByRole('option', { name: effort }));
    }
    await runWhenReady();
    await waitFor(() => expect(createAgentJob).toHaveBeenCalled());
    return vi.mocked(createAgentJob).mock.calls[0][0];
  }

  it('selects the deepest level on offer and sends it untouched', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(thinking);

    render(<TaskComposer />);
    expect(await screen.findByLabelText('Reasoning')).toHaveValue('max');
    await chooseRepository();
    fireEvent.change(await screen.findByRole('textbox'), { target: { value: 'do it' } });
    await runWhenReady();

    await waitFor(() => expect(createAgentJob).toHaveBeenCalled());
    expect(vi.mocked(createAgentJob).mock.calls[0][0]).toMatchObject({ reasoning_effort: 'max' });
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
    await chooseRepository();
    fireEvent.click(await screen.findByLabelText('Reasoning'));
    fireEvent.click(screen.getByRole('option', { name: 'low' }));
    // model-plain takes no effort at all, so the control goes away entirely —
    // and the pending selection must not survive to be submitted.
    fireEvent.click(screen.getByLabelText('Model'));
    fireEvent.click(screen.getByRole('option', { name: 'model-plain' }));

    await waitFor(() => expect(screen.queryByLabelText('Reasoning')).not.toBeInTheDocument());
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'do it' } });
    await runWhenReady();

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
    await chooseRepository();
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

  it('opens Add without leaving slash suggestions behind it', async () => {
    const box = await composer();
    fireEvent.change(box, { target: { value: '/go' } });
    expect(menu()).toBeInTheDocument();
    await untilIdle();
    fireEvent.click(screen.getByRole('button', { name: 'Add to message' }));
    expect(menu()).not.toBeInTheDocument();
    expect(screen.getByRole('menu')).toBeInTheDocument();
    expect(box).toHaveValue('/go');
    fireEvent.click(screen.getByRole('menuitem', { name: /Goal/ }));
    expect(box).toHaveValue('/goal ');
    expect(box).toHaveFocus();
    expect(createAgentJob).not.toHaveBeenCalled();
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
    await untilRunnable();
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
    await untilRunnable();
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
    await runWhenReady();

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
    await untilRunnable();
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
    await untilRunnable();
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
