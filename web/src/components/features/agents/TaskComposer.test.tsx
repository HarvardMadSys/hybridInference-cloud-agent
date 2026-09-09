// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createAgentJob, getAgentConfig, listRepoBranches } from '@/lib/api/agents';
import type { AgentConfigApi } from '@/lib/api/agents';

import { TaskComposer } from './TaskComposer';

const navigation = vi.hoisted(() => ({ push: vi.fn() }));

vi.mock('next/navigation', () => ({
  useRouter: () => navigation,
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
    setup_egress_tier: 'trusted',
    agent_egress_tier: 'platform_only',
    github_connected: true,
    github_install_url: null,
    ...overrides,
  };
}

describe('TaskComposer source-control onboarding', () => {
  beforeEach(() => {
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
