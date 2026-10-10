'use client';

// The admin area's own gate, so a page added under `/admin` is refused by
// default rather than guarded only if its author remembers. The per-page
// guards stay: this layout is fail-safe for new pages, not a replacement for
// the checks that key views on the account.
//
// Client-side, deliberately. The control plane enforces every `/admin/agent/*`
// call — an unauthorized visitor here sees this notice instead of data, and
// the guarded fetches beneath would refuse them anyway.

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const { state } = useAuth();
  // Until the permission answer is in, this layout owns the refusal: render
  // nothing rather than flash the admin area at a visitor still being decided.
  // (ProtectedRoute also withholds children while it loads; this is the
  // layout's own statement, not a duplicate of that.)
  if (state.loading || !hasAgentPermission(state.user?.agent_access, 'agent.admin')) {
    return null;
  }
  return <>{children}</>;
}
