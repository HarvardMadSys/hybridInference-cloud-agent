'use client';

import { useCallback, useState } from 'react';

import { useAuth } from '@/components/providers';

import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import { AdminRequired } from '../runs/shared';
import { SandboxResults } from './SandboxResults';

export default function AdminSandboxesPage() {
  const { state } = useAuth();
  return <SandboxesView key={state.user?.id ?? 'admin'} />;
}

function SandboxesView() {
  const [denied, setDenied] = useState(false);
  const onDenied = useCallback(() => setDenied(true), []);
  if (denied)
    return <AdminRequired>Sandboxes across users are visible to admins only.</AdminRequired>;
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
