// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { WaitlistPanel } from './WaitlistPanel';
import * as api from '@/lib/api/accounts';
import type { WaitlistEntry } from '@/lib/api/accounts';

const auth = vi.hoisted(() => ({
  state: {
    user: {
      id: 'owner',
      email: 'owner@example.test',
      auth_mode: 'local',
      waitlist: undefined as WaitlistEntry | undefined,
    },
  },
  refreshUser: vi.fn(),
  logout: vi.fn(),
}));
vi.mock('@/components/providers/AuthProvider', () => ({ useAuth: () => auth }));
vi.mock('@/lib/api/accounts', () => ({ getWaitlist: vi.fn(), submitApplication: vi.fn() }));
const pending: WaitlistEntry = {
  status: 'pending',
  purpose: 'Research tools',
  review_reason: null,
  version: 1,
  submitted_at: '2026-10-28T00:00:00Z',
  reviewed_at: null,
};
beforeEach(() => {
  vi.resetAllMocks();
  auth.state.user.waitlist = undefined;
  auth.refreshUser.mockResolvedValue(undefined);
});
afterEach(cleanup);
describe('waiting list access states', () => {
  it('pending users see their application without an Agent entry or second application form', async () => {
    vi.mocked(api.getWaitlist).mockResolvedValue(pending);
    render(<WaitlistPanel />);
    await screen.findByText('Waiting for review');
    expect(screen.queryByRole('link', { name: 'Open Cloud Agent' })).toBeNull();
    expect(screen.queryByRole('textbox')).toBeNull();
    expect(screen.getByText('Research tools')).toBeInTheDocument();
  });
  it.each(['rejected', 'revoked'] as const)(
    '%s users can read a reason and explicitly reapply',
    async (status) => {
      vi.mocked(api.getWaitlist).mockResolvedValue({
        ...pending,
        status,
        review_reason: 'Please describe the project.',
      });
      vi.mocked(api.submitApplication).mockResolvedValue(pending);
      render(<WaitlistPanel />);
      await screen.findByText('Please describe the project.');
      fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Updated project' } });
      fireEvent.submit(
        screen.getByRole('button', { name: 'Submit a new application' }).closest('form')!,
      );
      await waitFor(() => expect(api.submitApplication).toHaveBeenCalledWith('Updated project'));
      expect(await screen.findByText('Waiting for review')).toBeInTheDocument();
      expect(auth.refreshUser).toHaveBeenCalled();
    },
  );
  it('refresh reflects approval and refreshes the current permissions', async () => {
    vi.mocked(api.getWaitlist)
      .mockResolvedValueOnce(pending)
      .mockResolvedValueOnce({ ...pending, status: 'approved' });
    render(<WaitlistPanel />);
    await screen.findByText('Waiting for review');
    fireEvent.click(screen.getByRole('button', { name: 'Refresh status' }));
    expect(await screen.findByRole('link', { name: 'Open Cloud Agent' })).toHaveAttribute(
      'href',
      '/',
    );
    expect(auth.refreshUser).toHaveBeenCalled();
  });
  it('failed reads do not invent an approved or pending state', async () => {
    vi.mocked(api.getWaitlist).mockRejectedValue(new Error('temporarily unavailable'));
    render(<WaitlistPanel />);
    expect(await screen.findByRole('alert')).toHaveTextContent('temporarily unavailable');
    expect(screen.queryByRole('link', { name: 'Open Cloud Agent' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Submit application' })).toBeNull();
  });
});

it('a slow initial status read does not erase a draft or undo a submitted application', async () => {
  const initial: WaitlistEntry = {
    ...pending,
    status: 'not_applied',
    purpose: '',
    version: 0,
    submitted_at: null,
  };
  auth.state.user.waitlist = initial;
  let resolve!: (value: WaitlistEntry) => void;
  vi.mocked(api.getWaitlist).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  vi.mocked(api.submitApplication).mockResolvedValue(pending);
  render(<WaitlistPanel />);
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'My project' } });
  fireEvent.submit(screen.getByRole('button', { name: 'Submit application' }).closest('form')!);
  await screen.findByText('Waiting for review');
  await act(async () => {
    resolve(initial);
  });
  expect(api.submitApplication).toHaveBeenCalledWith('My project');
  expect(screen.getByText('Waiting for review')).toBeInTheDocument();
});

it('a slow initial read preserves a draft before submission', async () => {
  const initial: WaitlistEntry = {
    ...pending,
    status: 'not_applied',
    purpose: '',
    version: 0,
    submitted_at: null,
  };
  auth.state.user.waitlist = initial;
  let resolve!: (value: WaitlistEntry) => void;
  vi.mocked(api.getWaitlist).mockReturnValue(
    new Promise((done) => {
      resolve = done;
    }),
  );
  render(<WaitlistPanel />);
  fireEvent.change(screen.getByRole('textbox'), { target: { value: 'My draft' } });
  await act(async () => {
    resolve(initial);
  });
  expect(screen.getByRole('textbox')).toHaveValue('My draft');
});
