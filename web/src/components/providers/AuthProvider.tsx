'use client';

// Session state for the app.
//
// Recreated rather than moved: the source provider drove the gateway's own
// email/password login and its token refresh. This service has neither. Sign-in
// happens at the gateway and lands as an HttpOnly cookie, so the only question
// this provider can answer is "does the cookie still work", which it asks by
// calling `/v1/session/me`.
//
import { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { AUTH_EXPIRED_EVENT } from '@/lib/api/client';
import { APIError } from '@/lib/utils/errors';
import {
  endSession,
  getSessionUser,
  type AgentAccessApi,
  type AgentPermission,
} from '@/lib/api/session';

interface User {
  id: string;
  email: string | null;
  user_name?: string | null;
  role: string;
  is_admin: boolean;
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

const SIGNED_OUT: AuthState = { isAuthenticated: false, loading: false, user: null };

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({
    isAuthenticated: false,
    loading: true,
    user: null,
  });

  const refreshUser = useCallback(async () => {
    setState((previous) => ({ ...previous, loading: true, error: null }));
    try {
      const me = await getSessionUser();
      if (!me) {
        setState(SIGNED_OUT);
        return;
      }
      setState({
        isAuthenticated: true,
        loading: false,
        user: {
          id: me.id,
          email: me.email ?? null,
          user_name: me.user_name,
          role: me.role || 'free',
          is_admin: hasAgentPermission(me.agent_access, 'agent.admin'),
          agent_access: me.agent_access,
        },
      });
    } catch (error) {
      // Do not retain permissions or mistake an outage for an expired session.
      const category =
        error instanceof APIError &&
        (error.code === 'agent_configuration_error' || error.statusCode === 404)
          ? 'configuration'
          : error instanceof APIError && error.code === 'invalid_session_response'
            ? 'invalid_response'
            : 'unavailable';
      setState({ ...SIGNED_OUT, error: category });
    }
  }, []);

  useEffect(() => {
    refreshUser().catch((error) => {
      console.error('Failed to refresh user:', error);
    });
  }, [refreshUser]);

  useEffect(() => {
    const handleAuthExpired = () => setState(SIGNED_OUT);
    window.addEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, handleAuthExpired);
  }, []);

  const logout = useCallback(async () => {
    try {
      await endSession();
    } finally {
      setState(SIGNED_OUT);
    }
  }, []);

  const value: AuthContextValue = { state, logout, refreshUser };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
}
