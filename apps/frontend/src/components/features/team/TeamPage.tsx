'use client';
// The people behind this deployment, from its site files (`site.yaml`), as
// freeinference.org's /team page lists its own. Distinct from the collaboration
// Teams under /settings/teams.
import { AccountFrame } from '@/components/features/auth/AccountFrame';
import { useLocale } from '@/components/providers/LocaleProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { localizedText, type TeamMember } from '@/config/site';

function MemberCard({ member }: { member: TeamMember }) {
  const { locale, t } = useLocale();
  const badge = localizedText(member.badge, locale);
  return (
    <li className="rounded-lg border border-border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-base font-semibold text-foreground">{member.name}</h3>
        {badge && (
          <span className="rounded-full bg-surface-muted px-2 py-0.5 text-xs text-muted">
            {badge}
          </span>
        )}
      </div>
      {member.roles.length > 0 && (
        <ul className="mt-1.5 space-y-0.5 text-sm text-muted">
          {member.roles.map((role, index) => (
            <li key={index}>{localizedText(role, locale)}</li>
          ))}
        </ul>
      )}
      {member.website && (
        <a
          className="ui-focus-ring mt-2 inline-block rounded text-sm text-accent underline"
          href={member.website}
          target="_blank"
          rel="noopener noreferrer"
        >
          {t('team.website')}
        </a>
      )}
    </li>
  );
}

export function TeamPage() {
  const { t } = useLocale();
  const { branding, content } = useSiteConfig();
  const team = content?.team ?? [];
  return (
    <AccountFrame
      title={t('team.title')}
      description={team.length ? t('team.description', { app_name: branding.appName }) : undefined}
    >
      {team.length ? (
        <ul className="space-y-4">
          {team.map((member) => (
            <MemberCard key={member.name} member={member} />
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted">{t('team.none')}</p>
      )}
    </AccountFrame>
  );
}
