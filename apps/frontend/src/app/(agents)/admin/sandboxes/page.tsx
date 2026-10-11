'use client';

import { useCallback, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';

import { AdminNavigation } from '../AdminNavigation';
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
      <section className="w-full px-4 py-10 sm:px-6 lg:px-8 lg:py-14">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="sandboxes" />
        <SandboxResults onDenied={onDenied} />
      </section>
    </div>
  );
}
