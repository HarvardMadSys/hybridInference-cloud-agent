'use client';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import { AdminRequired } from './runs/shared';

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const { state } = useAuth();
  // Withhold admin pages while access is checked, including during a refresh
  // that still holds the previous account's permissions. The parent layout
  // handles sign-in and session errors; this gate handles admin permission.
  if (state.loading) return null;
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) {
    return (
      <div lang="en">
        <AdminRequired />
      </div>
    );
  }
  return (
    <div lang="en" className="admin-canvas flex min-h-full min-w-0 flex-col">
      {children}
    </div>
  );
}
