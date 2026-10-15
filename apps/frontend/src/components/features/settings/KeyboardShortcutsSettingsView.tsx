'use client';

import { useT } from '@/components/providers';
import { KeyboardShortcutsSettings } from './KeyboardShortcutsSettings';
import { SettingsPage } from './SettingsPage';

export function KeyboardShortcutsSettingsView() {
  const t = useT();
  return (
    <SettingsPage
      title={t('settings.shortcuts.title')}
      description={t('settings.shortcuts.description')}
    >
      <KeyboardShortcutsSettings />
    </SettingsPage>
  );
}
