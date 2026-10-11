// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// Setup notices have their own tests; this page uses a configured deployment.
vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: async () => ({
    revision: 1,
    definitions: [],
    values: {},
    secret_configured: {},
    bootstrap: [],
    setup: { required: false, groups: [], bootstrap_missing: [] },
  }),
}));

import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { AdminStep, AdminStepsPage, AdminStepTurn } from '@/lib/api/adminRuns';
import { APIError } from '@/lib/utils/errors';

const context = vi.hoisted(() => ({ query: 'level=steps' }));
vi.mock('@/components/providers', () => ({
  useAuth: () => ({
    state: {
      user: {
        id: 'admin',
        agent_access: { allowed: true, permissions: ['agent.use', 'agent.admin'] },
      },
    },
  }),
}));
vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(context.query),
  useParams: () => ({}),
}));
vi.mock('@/lib/api/adminRuns', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/adminRuns')>()),
  getAdminRuns: vi.fn(),
  getAdminSteps: vi.fn(),
}));
vi.mock('@/lib/api/adminConversations', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/adminConversations')>()),
  getAdminConversations: vi.fn(),
}));

import { getAdminSteps } from '@/lib/api/adminRuns';
import { getAdminConversations } from '@/lib/api/adminConversations';
import List from './page';

const since = '2026-09-20T06:00:00Z';
const until = '2026-09-27T06:00:00Z';

function turn(overrides: Partial<AdminStepTurn> = {}): AdminStepTurn {
  return {
    job_id: 'job-2',
    thread_id: 'thread-1',
    turn_no: 2,
    user_id: 'owner',
    repo: 'org/repo',
    runtime: 'claude-code',
    state: 'succeeded',
    created_at: '2026-09-22T06:40:00Z',
    coverage: 'complete',
    reason: null,
    ...overrides,
  };
}

function step(overrides: Partial<AdminStep> = {}): AdminStep {
  return {
    request_id: 'req-1',
    request_started_at: '2026-09-22T06:41:10Z',
    logged_at: '2026-09-22T06:41:12Z',
    model: 'glm-5.1',
    served_model_id: null,
    tokens_in: 26000,
    tokens_out: 410,
    cache_read: 21000,
    cache_write: null,
    reasoning: null,
    spent_usd: '0.0108',
    ttft_ms: 900,
    latency_ms: 5200,
    status_code: 200,
    usage_estimated: false,
    job_id: 'job-2',
    attempt_no: 1,
    ...overrides,
  };
}

function page(overrides: Partial<AdminStepsPage> = {}): AdminStepsPage {
  return {
    since,
    until,
    fetched_at: until,
    source: 'gateway_api_logs',
    turns: [turn()],
    steps: [step()],
    next_cursor: null,
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  context.query = 'level=steps';
  vi.mocked(getAdminSteps).mockResolvedValue(page());
});
afterEach(cleanup);

describe('steps level', () => {
  it('lists each model request with the turn that made it', async () => {
    vi.mocked(getAdminSteps).mockResolvedValue(
      page({
        turns: [
          turn(),
          turn({ job_id: 'job-1', turn_no: 1, repo: 'org/other', user_id: 'someone' }),
        ],
        steps: [
          step({ request_id: 'req-new', request_started_at: '2026-09-22T06:48:10Z' }),
          step({ request_id: 'req-old', job_id: 'job-1', status_code: 429, tokens_out: null }),
        ],
      }),
    );
    render(<List />);
    const table = await screen.findByRole('table', { name: 'Steps' });
    expect(screen.getByRole('button', { name: 'Steps', pressed: true })).toBeInTheDocument();
    expect(getAdminConversations).not.toHaveBeenCalled();
    const [newest, older] = within(table).getAllByRole('row').slice(1);
    expect(within(newest).getByRole('link', { name: 'Turn 2' })).toHaveAttribute(
      'href',
      '/admin/runs/job-2',
    );
    expect(within(newest).getByTitle('org/repo')).toHaveTextContent('repo');
    expect(within(newest).getByText('26,000')).toBeInTheDocument();
    expect(within(newest).getByText('80.8%')).toBeInTheDocument();
    expect(within(older).getByRole('link', { name: 'Turn 1' })).toHaveAttribute(
      'href',
      '/admin/runs/job-1',
    );
    expect(within(older).getByText('someone')).toBeInTheDocument();
    expect(within(older).getByText('429')).toBeInTheDocument();
    // A missing count is not a zero.
    expect(within(older).getAllByText('—').length).toBeGreaterThan(0);
    // The selection is the page's: the seven-day window every level starts from.
    const filters = vi.mocked(getAdminSteps).mock.calls[0][0];
    expect(Date.parse(filters.until!) - Date.parse(filters.since!)).toBe(7 * 86_400_000);
  });

  it('names the turns whose requests were not all read, and why', async () => {
    vi.mocked(getAdminSteps).mockResolvedValue(
      page({
        turns: [
          turn(),
          turn({ job_id: 'job-3', turn_no: 3, coverage: 'partial' }),
          turn({
            job_id: 'job-4',
            turn_no: 4,
            coverage: 'unavailable',
            reason: 'gateway_unavailable',
          }),
          turn({ job_id: 'job-5', turn_no: 5, coverage: 'not_started' }),
        ],
      }),
    );
    render(<List />);
    await screen.findByRole('table', { name: 'Steps' });
    const partial = screen.getByText(/made more requests than a page lists/);
    expect(within(partial).getByRole('link', { name: 'Turn 3' })).toHaveAttribute(
      'href',
      '/admin/runs/job-3',
    );
    expect(screen.getByText(/is not listed: the gateway could not be read/)).toBeInTheDocument();
    // A turn that never started made no requests; there is nothing to explain.
    expect(screen.queryByRole('link', { name: 'Turn 5' })).not.toBeInTheDocument();
  });

  it('loads older turns after the ones on screen, in the same window', async () => {
    vi.mocked(getAdminSteps)
      .mockResolvedValueOnce(page({ next_cursor: 'next-turns' }))
      .mockResolvedValueOnce(
        page({
          turns: [turn({ job_id: 'job-1', turn_no: 1 })],
          steps: [step({ request_id: 'req-older', job_id: 'job-1' })],
        }),
      );
    render(<List />);
    fireEvent.click(await screen.findByRole('button', { name: 'Load more steps' }));
    await screen.findByRole('link', { name: 'Turn 1' });
    expect(vi.mocked(getAdminSteps).mock.lastCall![0]).toMatchObject({
      since,
      until,
      cursor: 'next-turns',
    });
    const rows = within(screen.getByRole('table', { name: 'Steps' })).getAllByRole('row');
    expect(rows).toHaveLength(3);
    expect(within(rows[2]).getByRole('link', { name: 'Turn 1' })).toBeInTheDocument();
  });

  it('says when no turn matches, and when the turns made no requests', async () => {
    vi.mocked(getAdminSteps).mockResolvedValueOnce(page({ turns: [], steps: [] }));
    const view = render(<List />);
    expect(await screen.findByText('No turns match these filters.')).toBeInTheDocument();
    view.unmount();
    vi.mocked(getAdminSteps).mockResolvedValueOnce(page({ steps: [] }));
    render(<List />);
    expect(
      await screen.findByText('These turns have no recorded model requests.'),
    ).toBeInTheDocument();
  });

  it('hands revoked access to the page instead of an empty list', async () => {
    vi.mocked(getAdminSteps).mockRejectedValue(new APIError('FORBIDDEN', 'revoked', 403));
    render(<List />);
    expect(await screen.findByText('Admin access required')).toBeInTheDocument();
  });

  it('keeps the selection when switching to steps from sessions', async () => {
    context.query = new URLSearchParams({ since, until, model: 'model-a' }).toString();
    vi.mocked(getAdminConversations).mockResolvedValue({
      conversations: [],
      since,
      until,
      next_cursor: null,
    });
    render(<List />);
    await waitFor(() => expect(getAdminConversations).toHaveBeenCalled());
    const selection = vi.mocked(getAdminConversations).mock.lastCall![0];
    fireEvent.click(screen.getByRole('button', { name: 'Steps' }));
    await waitFor(() => expect(getAdminSteps).toHaveBeenCalledTimes(1));
    expect(vi.mocked(getAdminSteps).mock.lastCall![0]).toEqual(selection);
  });
});
