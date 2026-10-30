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
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <AdminNavigation current="deployment" />
      <div className={ADMIN_PAGE}>
        <AdminPageHeader
          section="Deployment"
          description="Machines with a deployment agent, what they run, and the operations you can start on them. Every state shown here is reported by a process or an agent."
        />
        <DeploymentManagement key={state.user?.id ?? 'admin'} />
      </div>
    </div>
  );
}
