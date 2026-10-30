'use client';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import { DeploymentManagement } from './DeploymentManagement';

export default function AdminDeploymentPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) {
    return (
      <div className="flex flex-1 items-center justify-center p-6 text-sm text-gray-500 dark:text-gray-400">
        Admin access required.
      </div>
    );
  }
  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section="Deployment"
          description="Machines, releases, keys and the database, as each machine reports them."
        />
        <AdminNavigation current="deployment" />
        <DeploymentManagement key={state.user?.id ?? 'admin'} />
      </section>
    </div>
  );
}
