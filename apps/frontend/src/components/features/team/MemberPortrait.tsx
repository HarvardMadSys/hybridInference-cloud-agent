'use client';

import { useState } from 'react';
import { useLocale } from '@/components/providers/LocaleProvider';
import type { TeamMember } from '@/config/site';

export function MemberPortrait({ member }: { member: TeamMember }) {
  const { locale, t } = useLocale();
  const [failedUrl, setFailedUrl] = useState<string>();
  const names = member.name.trim().split(/\s+/);
  const initials =
    names.length > 1
      ? `${Array.from(names[0] ?? '')[0] ?? ''}${Array.from(names.at(-1) ?? '')[0] ?? ''}`
      : Array.from(member.name).slice(0, 2).join('');
  return (
    <div className="mb-6 flex h-28 w-28 shrink-0 items-center justify-center overflow-hidden rounded-full bg-gradient-to-br from-accent/10 to-accent/5 text-2xl font-semibold tracking-wide text-accent ring-4 ring-surface shadow-subtle sm:h-32 sm:w-32 sm:text-3xl">
      {member.photoUrl && member.photoUrl !== failedUrl ? (
        // Deployment-owned HTTPS/inline raster images need no server image proxy.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={member.photoUrl}
          alt={t('team.photoAlt', { name: member.name })}
          width={128}
          height={128}
          referrerPolicy="no-referrer"
          className="h-full w-full object-cover"
          onError={() => setFailedUrl(member.photoUrl)}
        />
      ) : (
        <span aria-hidden="true">{initials.toLocaleUpperCase(locale)}</span>
      )}
    </div>
  );
}
