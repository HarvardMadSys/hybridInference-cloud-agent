'use client';
// The people behind this deployment, from its site files (`site.yaml`), as
// freeinference.org's /team page lists its own. Distinct from the collaboration
// Teams under /settings/teams.
import { PublicPageFrame } from '@/components/PublicPageFrame';
import { useLocale } from '@/components/providers/LocaleProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { localizedText, type TeamMember } from '@/config/site';
import { MemberPortrait } from './MemberPortrait';

function MemberCard({ member }: { member: TeamMember }) {
  const { locale, t } = useLocale();
  const badge = localizedText(member.badge, locale);
  return (
    <li className="flex min-w-0 flex-col items-center rounded-2xl border border-border bg-surface px-6 py-9 text-center shadow-subtle sm:px-8 sm:py-10">
      <MemberPortrait member={member} />
      <h3 className="max-w-full break-words font-serif text-2xl font-semibold tracking-tight text-foreground">
        {member.name}
      </h3>
      {badge && (
        <span className="mt-3 max-w-full break-words rounded-full bg-accent/10 px-3 py-1 text-[11px] font-medium uppercase tracking-[0.06em] text-accent">
          {badge}
        </span>
      )}
      {member.roles.length > 0 && (
        <ul className="mt-5 max-w-full space-y-2 break-words text-sm leading-7 text-muted sm:text-[15px]">
          {member.roles.map((role, index) => (
            <li key={index}>{localizedText(role, locale)}</li>
          ))}
        </ul>
      )}
      {member.website && (
        <a
          className="ui-focus-ring mt-auto inline-flex items-center gap-1.5 rounded pt-6 text-sm font-medium text-accent underline-offset-4 hover:underline"
          href={member.website}
          target="_blank"
          rel="noopener noreferrer"
        >
          {t('team.website')}
          <span aria-hidden="true">↗</span>
        </a>
      )}
    </li>
  );
}

export function TeamPage() {
  const { t } = useLocale();
  const { branding, content } = useSiteConfig();
  const team = content?.team ?? [];
  const [headingBefore, headingAfter] = t('team.description').split('{app_name}');
  const columns =
    team.length === 1
      ? 'max-w-md'
      : team.length === 2
        ? 'max-w-3xl sm:grid-cols-2'
        : 'sm:grid-cols-2 lg:grid-cols-3';
  return (
    <PublicPageFrame className="bg-surface-muted/60">
      <main className="mx-auto w-full max-w-[1200px] flex-1 px-[18px] pb-16 pt-8 sm:px-7 sm:pb-20 sm:pt-14">
        <section
          aria-labelledby="team-heading"
          className="rounded-2xl border border-border/40 bg-gradient-to-br from-surface via-surface to-accent/[0.025] px-6 py-10 text-center shadow-subtle sm:rounded-3xl sm:px-10 sm:py-14"
        >
          <p className="text-xs font-semibold uppercase tracking-[0.18em] text-accent">
            {t('team.eyebrow')}
          </p>
          <h2
            id="team-heading"
            className="mx-auto mt-4 max-w-4xl break-words text-balance font-serif text-4xl font-semibold leading-[1.12] tracking-[-0.035em] text-foreground sm:text-5xl lg:text-[56px]"
          >
            {headingBefore}
            <span className="text-accent">{branding.appName}</span>
            {headingAfter}
          </h2>
          <p className="mx-auto mt-6 max-w-2xl text-balance text-base leading-7 text-muted sm:text-lg sm:leading-8">
            {t('team.intro')}
          </p>
        </section>
        {team.length ? (
          <ul
            aria-label={t('team.title')}
            className={`mx-auto mt-8 grid gap-6 sm:mt-10 ${columns}`}
          >
            {team.map((member) => (
              <MemberCard key={member.name} member={member} />
            ))}
          </ul>
        ) : (
          <p className="mt-10 text-center text-sm text-muted">{t('team.none')}</p>
        )}
      </main>
    </PublicPageFrame>
  );
}
