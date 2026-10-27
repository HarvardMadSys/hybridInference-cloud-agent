'use client';

import { useT } from '@/components/providers';
import { ModelSettings } from './ModelSettings';
import { ModelConnectionsSettings } from './ModelConnectionsSettings';
import { NotificationSettings } from './NotificationSettings';
import { ProfileSettings } from './ProfileSettings';
import { SettingsPage } from './SettingsPage';

export function MySettingsView() {
  const t = useT();
  return (
    <SettingsPage title={t('settings.title')} description={t('settings.personalDescription')}>
      <ProfileSettings />
      <ModelSettings />
      <ModelConnectionsSettings />
      <NotificationSettings />
    </SettingsPage>
  );
}
