'use client';

import { useT } from '@/components/providers';
import { ModelConnectionsSettings } from './ModelConnectionsSettings';
import { ProvidedModelsCard } from './ProvidedModelsCard';
import { SettingsPage } from './SettingsPage';

export function ModelsSettingsView() {
  const t = useT();
  return (
    <SettingsPage title={t('settings.models.title')} description={t('settings.models.description')}>
      <ProvidedModelsCard />
      <ModelConnectionsSettings />
    </SettingsPage>
  );
}
