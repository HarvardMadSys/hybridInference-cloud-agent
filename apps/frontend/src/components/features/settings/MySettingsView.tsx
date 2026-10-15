'use client';

import { useT } from '@/components/providers';
import { MemorySettings } from './MemorySettings';
import { NotificationSettings } from './NotificationSettings';
import { ProfileSettings } from './ProfileSettings';
import { SettingsPage } from './SettingsPage';

export function MySettingsView() {
  const t = useT();
  return (
    <SettingsPage title={t('settings.title')} description={t('settings.personalDescription')}>
      <ProfileSettings />
      <NotificationSettings />
      <div id="settings-user-memory">
        <MemorySettings />
      </div>
    </SettingsPage>
  );
}
