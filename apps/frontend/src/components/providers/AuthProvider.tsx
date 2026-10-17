'use client';

// Session state for the app.
//
// Recreated rather than moved: the source provider drove the gateway's own
// email/password login and its token refresh. This service has neither. Sign-in
// happens at the gateway and lands as an HttpOnly cookie, so the only question
// this provider can answer is "does the cookie still work", which it asks by
// calling `/v1/session/me`.
//
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { clearSideChatDrafts } from '@/components/features/agents/sideChatStorage';
import { clearAccountResources } from '@/components/features/profile/accountResource';
import { AUTH_EXPIRED_EVENT } from '@/lib/api/client';
import { announceSession, listenForSession } from '@/lib/auth/sessionSync';
import { APIError } from '@/lib/utils/errors';
import {
  endSession,
  getSessionUser,
  type AgentAccessApi,
  type AgentPermission,
} from '@/lib/api/session';

interface User {
  id: string;
  agent_access?: AgentAccessApi;
}

interface AuthState {
  isAuthenticated: boolean;
  loading: boolean;
  user: User | null;
  error?: 'unavailable' | 'configuration' | 'invalid_response' | null;
}

interface AuthContextValue {
  state: AuthState;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
  /**
   * True once this tab finds its session now belongs to a different account
   * than the one it was showing — another tab switched the browser's Agents
   * session — until `acknowledgeSessionSwitch` says the tab has dealt with it.
   * State rather than an event: the switch passes through the loading state,
   * which unmounts the pages that would hear an event.
   */
  sessionSwitched: boolean;
  acknowledgeSessionSwitch: () => void;
}

export function hasAgentPermission(
  access: AgentAccessApi | null | undefined,
  required: AgentPermission,
): boolean {
  if (!access || access.allowed !== true || !Array.isArray(access.permissions)) return false;
  const permissions = access.permissions;
  if (
    permissions.some((permission) => permission !== 'agent.use' && permission !== 'agent.admin') ||
    new Set(permissions).size !== permissions.length ||
    !permissions.includes('agent.use')
  ) {
    return false;
  }
  return permissions.includes(required);
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** How often looking at a tab again may re-check its session. */
const RECHECK_INTERVAL_MS = 5_000;

const SIGNED_OUT: AuthState = { isAuthenticated: false, loading: false, user: null };

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const accountId = useRef<string | undefined>(undefined);
  // Bumped by every read of the session and every sign-out this tab makes. A
  // reply to an older read describes a session that may no longer stand — a
  // sign-out or switch since then has the last word — so it is dropped.
  const sessionEpoch = useRef(0);
  // Whether the latest read is still unanswered.
  const reading = useRef(false);
  const [state, setState] = useState<AuthState>({
    isAuthenticated: false,
    loading: true,
    user: null,
  });
  const [sessionSwitched, setSessionSwitched] = useState(false);
  const acknowledgeSessionSwitch = useCallback(() => setSessionSwitched(false), []);

  const refreshUser = useCallback(async () => {
    const epoch = ++sessionEpoch.current;
    reading.current = true;
    setState((previous) => ({ ...previous, loading: true, error: null }));
    try {
      const me = await getSessionUser();
      if (epoch !== sessionEpoch.current) return;
      reading.current = false;
      if (!me) {
        clearAccountResources();
        accountId.current = undefined;
        setState(SIGNED_OUT);
        return;
      }
      const previous = accountId.current;
      if (previous !== me.id) {
        clearAccountResources();
        announceSession(me.id);
        if (previous !== undefined) setSessionSwitched(true);
      }
      accountId.current = me.id;
      setState({
        isAuthenticated: true,
        loading: false,
        user: {
          id: me.id,
          agent_access: me.agent_access,
        },
      });
    } catch (error) {
      if (epoch !== sessionEpoch.current) return;
      reading.current = false;
      // Do not retain permissions or mistake an outage for an expired session.
      const category =
        error instanceof APIError &&
        (error.code === 'agent_configuration_error' || error.statusCode === 404)
          ? 'configuration'
          : error instanceof APIError && error.code === 'invalid_session_response'
            ? 'invalid_response'
            : 'unavailable';
      clearAccountResources();
      accountId.current = undefined;
      setState({ ...SIGNED_OUT, error: category });
    }
  }, []);

  useEffect(() => {
    refreshUser().catch((error) => {
      console.error('Failed to refresh user:', error);
    });
  }, [refreshUser]);

  // Whether the cookie still names the account this tab shows, asked without
  // the loading state a full refresh shows, and acted on only when it does not.
  const lastCheck = useRef(0);
  const checkSession = useCallback(async () => {
    lastCheck.current = Date.now();
    try {
      const me = await getSessionUser();
      if ((me?.id ?? undefined) !== accountId.current) await refreshUser();
    } catch {
      // An outage is not a different account; the next request will say so.
    }
  }, [refreshUser]);

  useEffect(
    () =>
      listenForSession(({ userId }) => {
        // A read still unanswered may have been asked before this change, and
        // its reply would undo it: ask again, and let only that reply stand.
        if (reading.current) void refreshUser();
        else if ((userId ?? undefined) !== accountId.current) void checkSession();
      }),
    [checkSession, refreshUser],
  );

  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return;
      if (Date.now() - lastCheck.current < RECHECK_INTERVAL_MS) return;
      void checkSession();
    };
    document.addEventListener('visibilitychange', onVisible);
    window.addEventListener('focus', onVisible);
    return () => {
      document.removeEventListener('visibilitychange', onVisible);
      window.removeEventListener('focus', onVisible);
    };
  }, [checkSession]);

  useEffect(() => {
    const handleAuthExpired = () => {
      sessionEpoch.current += 1;
      reading.current = false;
      clearSideChatDrafts(state.user?.id);
      clearAccountResources();
      accountId.current = undefined;
      setState(SIGNED_OUT);
    };
    window.addEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
  }, [state.user?.id]);

  const logout = useCallback(async () => {
    // Signed out once the server has dropped the cookie, and not before. A
    // failed request leaves the session working, and a signed-out page over it
    // would hand this account to the next person at this browser. The caller
    // gets the failure and says so.
    await endSession();
    sessionEpoch.current += 1;
    reading.current = false;
    clearSideChatDrafts(state.user?.id);
    clearAccountResources();
    accountId.current = undefined;
    setState(SIGNED_OUT);
    announceSession(null);
  }, [state.user?.id]);

  const value: AuthContextValue = {
    state,
    logout,
    refreshUser,
    sessionSwitched,
    acknowledgeSessionSwitch,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** The signed-in account, for scoping what a component keeps; none outside the provider. */
export function useSignedInAccount(): string | undefined {
  return useContext(AuthContext)?.state.user?.id;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
}
