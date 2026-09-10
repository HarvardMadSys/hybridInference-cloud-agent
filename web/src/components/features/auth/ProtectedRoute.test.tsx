// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// F3's acceptance, both halves: an unauthenticated visitor is sent to the
// gateway with a PKCE challenge, and an authenticated one sees the app.
//
// The session BFF is mocked rather than served — what is being tested is the
// browser's half of the contract, and a real control plane would only add a
// second thing that can be wrong.

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/session', () => ({
  getSessionUser: vi.fn(),
  endSession: vi.fn(),
}));

import { getSessionUser } from '@/lib/api/session';
import { AuthProvider } from '@/components/providers/AuthProvider';
import { ProtectedRoute } from './ProtectedRoute';
import { APIError } from '@/lib/utils/errors';

const assign = vi.fn();

function stubLocation(pathname = '/task-1', search = '?tab=diff') {
  Object.defineProperty(window, 'location', {
    configurable: true,
    writable: true,
    value: { origin: 'https://agents.example.org', pathname, search, assign },
  });
}

function renderApp() {
  return render(
    <AuthProvider>
      <ProtectedRoute>
        <p>task list</p>
      </ProtectedRoute>
    </AuthProvider>,
  );
}

describe('ProtectedRoute', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    stubLocation();
  });
  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it.each(['direct', 'detail'])(
    'keeps configuration failures non-retryable through the real %s response parser',
    async (envelope) => {
      const sessionApi =
        await vi.importActual<typeof import('@/lib/api/session')>('@/lib/api/session');
      vi.mocked(getSessionUser).mockImplementation(sessionApi.getSessionUser);
      const body = {
        error: {
          type: 'agent_configuration_error',
          message: 'An invalid token is configured; the deployment credential has expired.',
        },
      };
      const fetchMock = vi.fn().mockResolvedValue(
        new Response(JSON.stringify(envelope === 'detail' ? { detail: body } : body), {
          status: 503,
        }),
      );
      vi.stubGlobal('fetch', fetchMock);

      renderApp();

      expect(await screen.findByRole('alert')).toHaveTextContent('not configured correctly');
      expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
      expect(screen.queryByText('task list')).not.toBeInTheDocument();
      expect(assign).not.toHaveBeenCalled();
      expect(fetchMock).toHaveBeenCalledTimes(1);
    },
  );

  it.each([
    [
      new APIError('agent_configuration_error', 'Configuration error', 503),
      'not configured correctly',
    ],
    [new APIError('UNKNOWN_ERROR', 'Not found', 404), 'not configured correctly'],
    [new APIError('invalid_session_response', 'Invalid response', 200), 'unexpected response'],
  ])('requires operator action for %s instead of offering a dead retry', async (error, message) => {
    vi.mocked(getSessionUser).mockRejectedValue(error);
    renderApp();
    expect(await screen.findByRole('alert')).toHaveTextContent(message as string);
    expect(assign).not.toHaveBeenCalled();
    expect(screen.queryByText('task list')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Try again' })).not.toBeInTheDocument();
    expect(getSessionUser).toHaveBeenCalledTimes(1);
  });

  it('offers retry after an outage without redirecting or rendering protected content', async () => {
    vi.mocked(getSessionUser).mockRejectedValueOnce(new Error('Gateway unavailable'));
    renderApp();

    expect(await screen.findByRole('alert')).toHaveTextContent('cannot confirm your access');
    expect(assign).not.toHaveBeenCalled();
    expect(screen.queryByText('task list')).not.toBeInTheDocument();

    vi.mocked(getSessionUser).mockResolvedValue({ id: 'u1' });
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(await screen.findByText('task list')).toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();
  });

  it("sends an unauthenticated visitor to this app's own login page", async () => {
    // Not straight at the control plane. Somebody who has never seen this
    // service should be told what it is and which authority they are about to
    // authenticate against, rather than arriving at a third party's consent
    // screen having been bounced through two redirects.
    vi.mocked(getSessionUser).mockResolvedValue(null);

    renderApp();

    await waitFor(() => expect(assign).toHaveBeenCalledOnce());
    const url = new URL(assign.mock.calls[0][0] as string, 'https://agents.example.org');

    expect(url.pathname).toBe('/login');
    expect(url.searchParams.get('return_to')).toBe('/task-1?tab=diff');
    expect(screen.queryByText('task list')).not.toBeInTheDocument();
  });

  it('builds no PKCE flow and parks nothing a script could read', async () => {
    // The regression this whole change exists to prevent. The verifier used to
    // be minted here and parked in `localStorage`, and the callback page
    // redeemed the code for the gateway's identity token — two bearer
    // credentials in page JavaScript. Both now live in the control plane, so
    // there is nothing in browser storage and nothing on the wire from here
    // but a landing path.
    vi.mocked(getSessionUser).mockResolvedValue(null);

    renderApp();

    await waitFor(() => expect(assign).toHaveBeenCalledOnce());
    const target = assign.mock.calls[0][0] as string;

    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    for (const leaked of ['code_challenge', 'code_verifier', 'client_id']) {
      expect(target).not.toContain(leaked);
    }
  });

  it('renders the app for a signed-in user and redirects nobody', async () => {
    vi.mocked(getSessionUser).mockResolvedValue({
      id: 'u1',
    });

    renderApp();

    expect(await screen.findByText('task list')).toBeInTheDocument();
    expect(assign).not.toHaveBeenCalled();
  });

  it('refuses to sign in rather than loop when the server says it cannot', async () => {
    // The browser cannot see the control plane's configuration, so it learns
    // this by being sent back with a flag. Asking again would be told the same
    // thing again — which is the loop this refusal exists to prevent.
    vi.mocked(getSessionUser).mockResolvedValue(null);
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { ...window.location, assign, search: '?sign_in_error=unavailable' },
    });

    renderApp();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Sign-in is not configured for this deployment.',
    );
    expect(assign).not.toHaveBeenCalled();
  });
});
