'use client';

import { useT } from '@/components/providers';
import { SettingsPage } from './SettingsPage';
import { UsageSettings } from './UsageSettings';

export function UsageSettingsView() {
  const t = useT();
  return (
    <SettingsPage title={t('settings.usage.title')} description={t('settings.usage.description')}>
      <UsageSettings />
    </SettingsPage>
  );
}
