'use client';

import { useT } from '@/components/providers';
import { useSignedInAuthMode } from '@/components/providers/AuthProvider';
import { ModelSettings } from './ModelSettings';
import { ModelConnectionsSettings } from './ModelConnectionsSettings';
import { NotificationSettings } from './NotificationSettings';
import { ProfileSettings } from './ProfileSettings';
import { SettingsPage } from './SettingsPage';

export function MySettingsView() {
  const t = useT();
  const local = useSignedInAuthMode() === 'local';
  return (
    <SettingsPage title={t('settings.title')} description={t('settings.personalDescription')}>
      <ProfileSettings />
      {local ? null : <ModelSettings />}
      <ModelConnectionsSettings />
      <NotificationSettings />
    </SettingsPage>
  );
}
