'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import { AdminNavigation } from '../AdminNavigation';
import { DatabaseExplorer } from './DatabaseExplorer';
import { PgAdminTools } from './PgAdminTools';

export default function AdminDatabasePage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) return <AccessDenied />;
  return <DatabaseView key={state.user?.id ?? 'admin'} />;
}

function AccessDenied() {
  return (
    <div className="flex flex-1 items-center justify-center p-6 text-sm text-gray-500 dark:text-gray-400">
      Admin access required.
    </div>
  );
}

function DatabaseView() {
  const [denied, setDenied] = useState(false);
  const requests = useRef(new Set<AbortController>());
  const trackRequest = useCallback((controller: AbortController) => {
    requests.current.add(controller);
    return () => requests.current.delete(controller);
  }, []);
  const onDenied = useCallback(() => {
    for (const controller of requests.current) controller.abort();
    setDenied(true);
  }, []);

  useEffect(() => {
    const active = requests.current;
    return () => {
      for (const controller of active) controller.abort();
    };
  }, []);

  if (denied) return <AccessDenied />;

  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className="mx-auto w-full max-w-7xl px-6 py-12 lg:px-12 lg:py-16">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="database" />
        <DatabaseExplorer trackRequest={trackRequest} onDenied={onDenied} />
        <PgAdminTools trackRequest={trackRequest} onDenied={onDenied} />
      </section>
    </div>
  );
}
