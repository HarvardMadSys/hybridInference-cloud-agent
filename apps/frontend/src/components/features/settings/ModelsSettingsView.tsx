'use client';

import { useT } from '@/components/providers';
import { ModelConnectionsSettings } from './ModelConnectionsSettings';
import { SettingsPage } from './SettingsPage';

export function ModelsSettingsView() {
  const t = useT();
  return (
    <SettingsPage title={t('settings.models.title')} description={t('settings.models.description')}>
      <ModelConnectionsSettings />
    </SettingsPage>
  );
}
