'use client';
import Link from 'next/link';
import { useAuth } from '@/components/providers/AuthProvider';
import { useT } from '@/components/providers/LocaleProvider';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { WaitlistPanel } from '@/components/features/auth/WaitlistPanel';
import { DeploymentFooter } from '@/components/DeploymentFooter';
function Content() {
  const { state } = useAuth();
  const t = useT();
  if (state.user?.auth_mode !== 'local')
    return (
      <div className="m-auto p-6">
        <Link className="ui-button ui-button-secondary" href="/">
          {t('waitlist.openAgent')}
        </Link>
      </div>
    );
  return <WaitlistPanel key={state.user.id} />;
}
export default function WaitingListPage() {
  return (
    <ProtectedRoute>
      <main className="flex flex-1 items-center">
        <Content />
      </main>
      <DeploymentFooter />
    </ProtectedRoute>
  );
}
