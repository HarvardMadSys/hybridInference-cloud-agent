'use client';

import { useT } from '@/components/providers';
import { MemorySettings } from './MemorySettings';
import { SettingsPage } from './SettingsPage';

export function MemorySettingsView() {
  const t = useT();
  return (
    <SettingsPage title={t('settings.memory.title')} description={t('settings.memory.description')}>
      <MemorySettings />
    </SettingsPage>
  );
}
