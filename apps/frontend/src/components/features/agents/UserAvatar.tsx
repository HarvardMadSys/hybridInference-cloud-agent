'use client';

import { useAuth } from '@/components/providers';
import { AvatarImage, PokeBall } from '@/components/features/profile/AvatarImage';
import { useUserProfile } from '@/components/features/profile/useUserProfile';
import type { UserMemoryView } from '@/lib/api/userMemory';
import { useUserHomeProfile } from './useUserHomeProfile';

export function UserAvatar({ view }: { view?: UserMemoryView | null }) {
  const { state } = useAuth();
  const userId = state.user?.id;
  if (view !== undefined)
    return <ProfileAvatar key={userId ?? 'signed-out'} userId={userId} view={view} />;
  return <AccountAvatar key={userId ?? 'signed-out'} userId={userId} />;
}

function AccountAvatar({ userId }: { userId?: string }) {
  const { view } = useUserHomeProfile(userId);
  return <ProfileAvatar userId={userId} view={view} />;
}

function ProfileAvatar({ userId, view }: { userId?: string; view: UserMemoryView | null }) {
  const { profile, loading } = useUserProfile(userId);
  const imageDataUrl = profile?.image_data_url;
  const companionDataUrl = loading ? undefined : view?.avatar?.data_url;
  const companion = !imageDataUrl;

  return (
    <div className="relative mx-auto mb-8 h-[152px] w-[152px]">
      <span
        aria-hidden="true"
        className="absolute inset-0 -rotate-6 rounded-[36px] border border-accent/20 bg-accent/5"
      />
      <div className="absolute inset-1 h-36 w-36 overflow-hidden rounded-[32px] border border-border bg-surface shadow-sm">
        <AvatarImage imageDataUrl={imageDataUrl} companionDataUrl={companionDataUrl} />
      </div>
      {companion ? (
        <svg
          aria-hidden="true"
          className="absolute -right-1 -top-1 h-7 w-7 text-accent"
          viewBox="0 0 20 20"
          fill="currentColor"
        >
          <path d="m8 1 1.8 5.2L15 8l-5.2 1.8L8 15l-1.8-5.2L1 8l5.2-1.8L8 1Z" />
          <circle cx="16.5" cy="16.5" r="1.5" opacity=".55" />
        </svg>
      ) : null}
      {companion && companionDataUrl ? (
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
