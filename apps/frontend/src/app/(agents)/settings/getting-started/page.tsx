'use client';

import Link from 'next/link';
import { useT } from '@/components/providers';
import { GettingStartedSettings } from '@/components/features/settings/GettingStartedSettings';
import { SettingsPage } from '@/components/features/settings/SettingsPage';
import { SETTINGS_PATHS } from '@/components/features/settings/navigation';

export default function GettingStartedPage() {
  const t = useT();
  return (
    <SettingsPage title={t('gettingStarted.title')} description={t('settings.helpDescription')}>
      <ol className="mt-6 space-y-4 text-sm leading-6 text-muted">
        <li>
          <Link
            href={SETTINGS_PATHS.integrations}
            className="ui-focus-ring rounded text-foreground underline underline-offset-4"
          >
            {t('settings.helpConnect')}
          </Link>
        </li>
        <li>
          <Link
            href={SETTINGS_PATHS.environments}
            className="ui-focus-ring rounded text-foreground underline underline-offset-4"
          >
            {t('settings.helpEnvironment')}
          </Link>
        </li>
        <li>
          <Link
            href="/"
            className="ui-focus-ring rounded text-foreground underline underline-offset-4"
          >
            {t('settings.helpChat')}
          </Link>
        </li>
      </ol>
      <GettingStartedSettings />
    </SettingsPage>
  );
}
