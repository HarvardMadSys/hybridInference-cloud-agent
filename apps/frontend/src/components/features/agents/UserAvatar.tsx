'use client';

import { useAuth, useT } from '@/components/providers';
import type { UserMemoryView } from '@/lib/api/userMemory';
import { useUserHomeProfile } from './useUserHomeProfile';

export function UserAvatar({ view }: { view?: UserMemoryView | null }) {
  const { state } = useAuth();
  const userId = state.user?.id;
  if (view !== undefined) return <AvatarImage dataUrl={view?.avatar.data_url} />;
  return <AccountAvatar key={userId ?? 'signed-out'} userId={userId} />;
}

function AccountAvatar({ userId }: { userId?: string }) {
  const { view } = useUserHomeProfile(userId);
  return <AvatarImage dataUrl={view?.avatar.data_url} />;
}

function AvatarImage({ dataUrl }: { dataUrl?: string }) {
  const t = useT();

  return (
    <div className="relative mx-auto mb-8 h-[152px] w-[152px]">
      <span
        aria-hidden="true"
        className="absolute inset-0 -rotate-6 rounded-[36px] border border-accent/20 bg-accent/5"
      />
      {dataUrl ? (
        // The API assembles the account's Pokémon illustration as a data URL.
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={dataUrl}
          alt={t('composer.avatarAlt')}
          title={t('composer.avatarDescription')}
          width={144}
          height={144}
          className="absolute inset-1 h-36 w-36 rounded-[32px] border border-border bg-surface object-contain shadow-sm"
        />
      ) : (
        <div
          aria-hidden="true"
          className="absolute inset-1 flex items-center justify-center rounded-[32px] border border-border bg-surface-muted shadow-sm"
        >
          <PokeBall className="h-16 w-16" />
        </div>
      )}
      <svg
        aria-hidden="true"
        className="absolute -right-1 -top-1 h-7 w-7 text-accent"
        viewBox="0 0 20 20"
        fill="currentColor"
      >
        <path d="m8 1 1.8 5.2L15 8l-5.2 1.8L8 15l-1.8-5.2L1 8l5.2-1.8L8 1Z" />
        <circle cx="16.5" cy="16.5" r="1.5" opacity=".55" />
      </svg>
      {dataUrl ? (
        <span
          aria-hidden="true"
          className="absolute -bottom-1 -right-1 flex h-9 w-9 items-center justify-center rounded-full border border-border bg-surface shadow-sm"
        >
          <PokeBall className="h-6 w-6" />
        </span>
      ) : null}
    </div>
  );
}

function PokeBall({ className }: { className: string }) {
  return (
    <svg className={className} viewBox="0 0 32 32" fill="none">
      <circle cx="16" cy="16" r="14" fill="#FFF9EB" />
      <path d="M2 16a14 14 0 0 1 28 0H2Z" fill="#E46965" />
      <circle cx="16" cy="16" r="14" stroke="#3D3946" strokeWidth="2" />
      <path d="M2 16h28" stroke="#3D3946" strokeWidth="2" />
      <circle cx="16" cy="16" r="4.5" fill="#FFF9EB" stroke="#3D3946" strokeWidth="2" />
      <circle cx="16" cy="16" r="1.5" fill="#3D3946" />
      <path d="M8 10a10 10 0 0 1 4-3" stroke="white" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}
