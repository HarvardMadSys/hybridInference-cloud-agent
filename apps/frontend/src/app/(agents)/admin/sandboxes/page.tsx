'use client';

import { useCallback, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';

import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import { SandboxResults } from './SandboxResults';

function AdminRequired() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8">
      <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
        Admin access required
      </h1>
      <p className="text-sm text-gray-500 dark:text-gray-400">
        Sandboxes across users are visible to admins only.
      </p>
    </div>
  );
}

export default function AdminSandboxesPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AdminRequired />;
  return <SandboxesView key={state.user?.id ?? 'admin'} />;
}

function SandboxesView() {
  const [denied, setDenied] = useState(false);
  const onDenied = useCallback(() => setDenied(true), []);
  if (denied) return <AdminRequired />;
  return (
    <div className="min-w-0 flex-1">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section="Sandboxes"
          description="Inspect retained environments, workload, and resource usage across servers."
        />
        <AdminNavigation current="sandboxes" />
        <SandboxResults onDenied={onDenied} />
      </section>
    </div>
  );
}
