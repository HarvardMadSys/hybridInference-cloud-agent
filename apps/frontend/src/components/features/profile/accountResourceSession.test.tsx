// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AuthProvider, useAuth } from '@/components/providers/AuthProvider';
import { AUTH_EXPIRED_EVENT } from '@/lib/api/client';
import { endSession, getSessionUser } from '@/lib/api/session';
import { createAccountResource } from './accountResource';

vi.mock('@/lib/api/session', () => ({ getSessionUser: vi.fn(), endSession: vi.fn() }));

const load = vi.fn<(signal: AbortSignal) => Promise<string>>();
const resource = createAccountResource((signal) => load(signal));

function Consumer() {
  const { state, logout, refreshUser } = useAuth();
  const { data } = resource.useResource(state.user?.id);
  return (
    <>
      <output data-testid="account">{state.user?.id ?? 'signed-out'}</output>
      <output data-testid="cached">{data ?? 'empty'}</output>
      <button onClick={() => void logout().catch(() => {})}>Sign out</button>
      <button onClick={() => void refreshUser()}>Refresh</button>
    </>
  );
}

async function open() {
  render(
    <AuthProvider>
      <Consumer />
    </AuthProvider>,
  );
  await waitFor(() => expect(screen.getByTestId('cached')).toHaveTextContent('Alice private data'));
}

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(getSessionUser).mockResolvedValue({ id: 'alice' });
  vi.mocked(endSession).mockResolvedValue(undefined);
  load.mockResolvedValue('Alice private data');
});
afterEach(cleanup);

describe('account cache session lifetime', () => {
  it.each(['sign-out', 'expired', 'missing-session', 'unavailable-session'])(
    'drops cached data after %s, including when the same account signs back in',
    async (transition) => {
      await open();
      if (transition === 'sign-out') fireEvent.click(screen.getByText('Sign out'));
      else if (transition === 'expired')
        act(() => window.dispatchEvent(new Event(AUTH_EXPIRED_EVENT)));
      else {
        if (transition === 'missing-session') vi.mocked(getSessionUser).mockResolvedValueOnce(null);
        else vi.mocked(getSessionUser).mockRejectedValueOnce(new Error('Unavailable'));
        fireEvent.click(screen.getByText('Refresh'));
      }
      await waitFor(() => expect(screen.getByTestId('account')).toHaveTextContent('signed-out'));
      load.mockReturnValueOnce(new Promise(() => {}));
      fireEvent.click(screen.getByText('Refresh'));
      await waitFor(() => expect(screen.getByTestId('account')).toHaveTextContent('alice'));
      expect(screen.getByTestId('cached')).toHaveTextContent('empty');
      expect(load).toHaveBeenCalledTimes(2);
    },
  );

  it('retains cached data when refreshing the same authenticated account', async () => {
    await open();
    fireEvent.click(screen.getByText('Refresh'));
    await act(async () => {});
    expect(screen.getByTestId('cached')).toHaveTextContent('Alice private data');
    expect(load).toHaveBeenCalledTimes(1);
  });

  it('retains cached data when signing out fails', async () => {
    await open();
    vi.mocked(endSession).mockRejectedValueOnce(new Error('Unavailable'));
    fireEvent.click(screen.getByText('Sign out'));
    await act(async () => {});
    expect(screen.getByTestId('account')).toHaveTextContent('alice');
    expect(screen.getByTestId('cached')).toHaveTextContent('Alice private data');
  });

  it('clears previous accounts and rejects a response from a superseded account', async () => {
    await open();
    let resolveBob!: (value: string) => void;
    load.mockReturnValueOnce(
      new Promise((resolve) => {
        resolveBob = resolve;
      }),
    );
    vi.mocked(getSessionUser).mockResolvedValueOnce({ id: 'bob' });
    fireEvent.click(screen.getByText('Refresh'));
    await waitFor(() => expect(screen.getByTestId('account')).toHaveTextContent('bob'));
    expect(screen.getByTestId('cached')).toHaveTextContent('empty');
    const bobSignal = load.mock.calls[1][0];
    load.mockReturnValueOnce(new Promise(() => {}));
    fireEvent.click(screen.getByText('Refresh'));
    await waitFor(() => expect(screen.getByTestId('account')).toHaveTextContent('alice'));
    expect(bobSignal.aborted).toBe(true);
    expect(screen.getByTestId('cached')).toHaveTextContent('empty');
    await act(async () => resolveBob('Bob private data'));
    expect(screen.getByTestId('cached')).toHaveTextContent('empty');
  });
});
