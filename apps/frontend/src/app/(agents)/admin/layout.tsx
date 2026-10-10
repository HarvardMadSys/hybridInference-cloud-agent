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
  // While the session loads, neither branch is true yet: render nothing rather
  // than flash "admin access required" at a signed-in admin.
  if (!state.loading && !hasAgentPermission(state.user?.agent_access, 'agent.admin')) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
          Admin access required
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          These pages are visible to admins only.
        </p>
      </div>
    );
  }
  return <>{children}</>;
}
