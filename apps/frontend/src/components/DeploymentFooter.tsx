'use client';

import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/LocaleProvider';

export function DeploymentFooter() {
  const { deployment } = useSiteConfig();
  const t = useT();
  const version = deployment?.version ?? '';
  const shortVersion = /^[a-f\d]{40}$/i.test(version) ? version.slice(0, 7) : version;
  const deployedAt = deployment?.deployedAt;

  return (
    <footer className="flex shrink-0 flex-wrap items-center justify-center gap-x-4 gap-y-1 border-t border-gray-200/80 bg-gray-50 px-4 py-2 text-xs text-gray-500 dark:border-white/10 dark:bg-gray-950 dark:text-gray-400">
      <span>
        {t('deployment.version')}:{' '}
        <span title={version || undefined}>{shortVersion || t('deployment.unavailable')}</span>
      </span>
      <span>
        {t('deployment.date')}:{' '}
        {deployedAt ? (
          <time dateTime={deployedAt} title={deployedAt}>
            {deployedAt.slice(0, 10)} UTC
          </time>
        ) : (
          t('deployment.unavailable')
        )}
      </span>
    </footer>
  );
}
