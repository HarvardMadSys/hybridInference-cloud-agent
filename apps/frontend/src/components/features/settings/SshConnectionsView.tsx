'use client';

import { useT } from '@/components/providers';
import { SshServersSection } from '@/components/features/agents/ssh/SshServersSection';
import { SettingsPage } from './SettingsPage';

export function SshConnectionsView() {
  const t = useT();
  return (
    <SettingsPage title={t('settings.ssh')} description={t('ssh.description')}>
      <SshServersSection />
    </SettingsPage>
  );
}
