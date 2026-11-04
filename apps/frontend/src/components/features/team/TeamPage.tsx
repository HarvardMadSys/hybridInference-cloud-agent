'use client';
// The people behind this site, as freeinference.org's /team page lists its own.
// Distinct from the collaboration Teams under /settings/teams.
import { AccountFrame } from '@/components/features/auth/AccountFrame';
import { useT } from '@/components/providers/LocaleProvider';
import type { TranslationKey } from '@/lib/i18n/dictionaries/en';

interface Member {
  name: string;
  badge: TranslationKey;
  roles: TranslationKey[];
  website: string;
}

const MEMBERS: Member[] = [
  {
    name: 'Juncheng Yang',
    badge: 'team.lead',
    roles: ['team.juncheng.role'],
    website: 'https://junchengyang.com/',
  },
  {
    name: 'Murphy Tian',
    badge: 'team.coreDeveloper',
    roles: ['team.murphy.role', 'team.murphy.school'],
    website: 'https://realtmxi.github.io/',
  },
];

export function TeamPage() {
  const t = useT();
  return (
    <AccountFrame title={t('team.title')} description={t('team.description')}>
      <ul className="space-y-4">
        {MEMBERS.map((member) => (
          <li key={member.name} className="rounded-lg border border-border p-4">
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="text-base font-semibold text-foreground">{member.name}</h3>
              <span className="rounded-full bg-surface-muted px-2 py-0.5 text-xs text-muted">
                {t(member.badge)}
              </span>
            </div>
            <ul className="mt-1.5 space-y-0.5 text-sm text-muted">
              {member.roles.map((role) => (
                <li key={role}>{t(role)}</li>
              ))}
            </ul>
            <a
              className="ui-focus-ring mt-2 inline-block rounded text-sm text-accent underline"
              href={member.website}
              target="_blank"
              rel="noopener noreferrer"
            >
              {t('team.website')}
            </a>
          </li>
        ))}
      </ul>
    </AccountFrame>
  );
}
