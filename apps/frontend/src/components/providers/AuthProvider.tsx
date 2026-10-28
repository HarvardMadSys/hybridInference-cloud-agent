'use client';

// Session state for the app.
//
// Both local and Gateway sign-in establish an HttpOnly cookie. Identity and
// current admission are read through /v1/session/me; no password or token is
// retained by this provider.
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
  type SessionUserApi,
} from '@/lib/api/session';

type User = SessionUserApi;

/** Admit only the documented identity fields, never legacy Gateway role claims. */
function sessionIdentity(me: SessionUserApi): User {
  const user: User = { id: me.id, agent_access: me.agent_access };
  if (me.auth_mode === 'local') {
    user.auth_mode = 'local';
    user.email = me.email;
    user.waitlist = me.waitlist;
  } else if (me.auth_mode === 'gateway') user.auth_mode = 'gateway';
  return user;
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
        user: sessionIdentity(me),
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
  const checkEpoch = useRef(0);
  const checkSession = useCallback(async () => {
    lastCheck.current = Date.now();
    const epoch = sessionEpoch.current;
    const check = ++checkEpoch.current;
    try {
      const me = await getSessionUser();
      if (epoch !== sessionEpoch.current || check !== checkEpoch.current || reading.current) return;
      if ((me?.id ?? undefined) !== accountId.current) await refreshUser();
      else if (me?.auth_mode === 'local') {
        // Local admission can change while identity stays the same. Apply the
        // fresh answer without unmounting the page or losing an edited draft.
        setState((previous) => ({ ...previous, user: sessionIdentity(me) }));
      }
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

/** Auth mode for optional navigation, including consumers outside the provider. */
export function useSignedInAuthMode(): SessionUserApi['auth_mode'] {
  return useContext(AuthContext)?.state.user?.auth_mode;
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
