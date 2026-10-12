'use client';

import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { useT } from '@/components/providers/LocaleProvider';

// Eastern time (America/New_York) so the shown time tracks daylight saving (EST/EDT).
const easternTime = new Intl.DateTimeFormat('en-US', {
  timeZone: 'America/New_York',
  hour: 'numeric',
  minute: '2-digit',
  timeZoneName: 'short',
});

export function DeploymentFooter() {
  const { deployment } = useSiteConfig();
  const t = useT();
  const version = deployment?.version ?? '';
  const shortVersion = /^[a-f\d]{40}$/i.test(version) ? version.slice(0, 7) : version;
  const deployedAt = deployment?.deployedAt;
  const deployedDate = deployedAt ? new Date(deployedAt).toISOString().slice(0, 10) : null;
  const deployedTimeEastern = deployedAt ? easternTime.format(new Date(deployedAt)) : null;

  return (
    <footer className="flex shrink-0 flex-wrap items-center justify-center gap-x-3 gap-y-0.5 border-t border-border bg-surface-muted px-3 py-1 text-[11px] text-muted">
      <a
        href="https://seas.harvard.edu/"
        target="_blank"
        rel="noopener noreferrer"
        className="hover:underline focus-visible:underline"
      >
        Harvard SEAS
      </a>
      <a
        href="https://madsys.seas.harvard.edu/"
        target="_blank"
        rel="noopener noreferrer"
        className="hover:underline focus-visible:underline"
      >
        MadSys Group
      </a>
      <span>
        {t('deployment.version')}:{' '}
        <span title={version || undefined}>{shortVersion || t('deployment.unavailable')}</span>
      </span>
      <span>
        {t('deployment.date')}:{' '}
        {deployedAt ? (
          <time dateTime={deployedAt} title={deployedAt}>
            {deployedDate} UTC · {deployedTimeEastern}
          </time>
        ) : (
          t('deployment.unavailable')
        )}
      </span>
    </footer>
  );
}
