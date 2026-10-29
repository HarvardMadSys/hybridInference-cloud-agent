'use client';
import { AccountSecurity } from '@/components/features/auth/AccountSecurity';
import { SettingsPage } from '@/components/features/settings/SettingsPage';
import { useAuth, useT } from '@/components/providers';

export default function AccountSettingsPage() {
  const t = useT();
  const { state } = useAuth();
  return (
    <SettingsPage title={t('security.title')} description={t('security.description')}>
      {state.user?.auth_mode === 'local' ? (
        <AccountSecurity isAdmin={state.user.is_local_admin === true} />
      ) : (
        <p className="text-sm text-muted">{t('security.gatewayManaged')}</p>
      )}
    </SettingsPage>
  );
}
