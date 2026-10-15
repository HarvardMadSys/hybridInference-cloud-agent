'use client';

import { useT } from '@/components/providers';
import { EnvironmentManager } from '@/components/features/agents/environment/EnvironmentManager';
import { SettingsPage } from './SettingsPage';

export function EnvironmentsSettingsView() {
  const t = useT();
  return (
    <SettingsPage
      title={t('settings.environments')}
      description={t('settings.environmentsDescription')}
    >
      <EnvironmentManager />
    </SettingsPage>
  );
}
