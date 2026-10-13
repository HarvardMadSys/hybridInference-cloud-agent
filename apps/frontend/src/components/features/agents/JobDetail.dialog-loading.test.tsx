// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';

import { cancelAgentJob } from '@/lib/api/agents';
import { listSkills } from '@/lib/api/skills';
import { JobDetail } from './JobDetail';
import type { AgentJob } from './types';

const skillLoad = vi.hoisted(() => {
  let resolve!: () => void;
  const pending = new Promise<void>((done) => {
    resolve = done;
  });
  return { pending, resolve };
});

vi.mock('./skills/SkillPicker', async (importOriginal) => {
  await skillLoad.pending;
  return importOriginal<typeof import('./skills/SkillPicker')>();
});

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn() }),
  usePathname: () => '/job-running',
}));

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  cancelAgentJob: vi.fn(),
  getAgentConfig: vi.fn().mockResolvedValue({
    repos: ['owner/repository'],
    runtimes: ['claude-code'],
    models: ['qwen-test'],
    github_connected: true,
  }),
  getAgentJobUsage: vi.fn().mockResolvedValue(null),
  refreshAgentJobUsage: vi.fn().mockResolvedValue(null),
  getAgentThreadStats: vi.fn().mockResolvedValue(null),
}));

vi.mock('@/lib/api/mcp', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/mcp')>()),
  listMcpInvocations: vi.fn().mockResolvedValue({ invocations: [] }),
  getMcpThreadBindings: vi.fn().mockResolvedValue({ bindings: [] }),
  listMcpConnections: vi.fn().mockResolvedValue({ connections: [] }),
}));

vi.mock('@/lib/api/questions', () => ({
  listThreadQuestions: vi.fn().mockResolvedValue({ questions: [], attempt_id: 1 }),
  answerThreadQuestion: vi.fn(),
}));

vi.mock('@/lib/api/skills', () => ({ listSkills: vi.fn().mockResolvedValue([]) }));

vi.mock('@/lib/api/pendingMessages', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/pendingMessages')>()),
  getThreadQueue: vi.fn().mockResolvedValue({
    thread_id: 'thread-running',
    pending_messages: [],
    queue_paused_reason: null,
    latest_job_id: 'job-running',
    latest_turn_no: 1,
    latest_job_state: 'running',
    mid_turn_input_supported: true,
  }),
}));

const job: AgentJob = {
  id: 'job-running',
  threadId: 'thread-running',
  title: 'Current task',
  prompt: 'Fix the issue',
  state: 'running',
  repo: 'owner/repository',
  baseRef: 'dev',
  baseSha: 'abcdef1',
  branch: 'agent/task',
  runtime: 'claude-code',
  model: 'qwen-test',
  spentUsd: 0,
  hasLedger: false,
  timeoutLabel: '',
  sandbox: 'container',
  attempts: [{ no: 1, status: 'live' }],
  currentAttemptNo: 1,
  events: [],
  diffFiles: [],
  diffStat: { add: 0, del: 0 },
  gates: [],
  usage: { tokensIn: '0', tokensOut: '0', cachePct: 0, turns: 1 },
};

afterEach(() => cleanup());

it('owns focus and Escape while the skills dialog downloads without stopping the running job', async () => {
  render(<JobDetail job={job} />);
  const opener = screen.getByRole('button', { name: 'Add to message' });
  const openSkills = () => {
    opener.focus();
    fireEvent.click(opener);
    fireEvent.click(screen.getByRole('menuitem', { name: /Skills\s*0/ }));
  };

  openSkills();
  let dialog = screen.getByRole('dialog', { name: 'Skills' });
  expect(within(dialog).getByRole('status')).toHaveTextContent('Loading');
  expect(dialog).toContainElement(document.activeElement as HTMLElement);
  expect(listSkills).not.toHaveBeenCalled();
  fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(cancelAgentJob).not.toHaveBeenCalled();

  openSkills();
  dialog = screen.getByRole('dialog', { name: 'Skills' });
  fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(cancelAgentJob).not.toHaveBeenCalled();

  openSkills();
  await act(async () => {
    skillLoad.resolve();
    await skillLoad.pending;
  });
  await screen.findByRole('button', { name: 'Apply' });
  expect(listSkills).toHaveBeenCalledTimes(1);
  fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(cancelAgentJob).not.toHaveBeenCalled();
});
