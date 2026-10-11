'use client';

import { useEffect, useState } from 'react';
import { useAuth, useT } from '@/components/providers';
import { getUserMemory } from '@/lib/api/userMemory';

export function UserAvatar() {
  const { state } = useAuth();
  const userId = state.user?.id;
  return <AccountAvatar key={userId ?? 'signed-out'} userId={userId} />;
}

function AccountAvatar({ userId }: { userId?: string }) {
  const t = useT();
  const [dataUrl, setDataUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!userId) return;
    const controller = new AbortController();
    void getUserMemory(controller.signal)
      .then((view) => {
        if (!controller.signal.aborted) setDataUrl(view.avatar.data_url);
      })
      // The composer stays usable if the avatar cannot be loaded.
      .catch(() => {});
    return () => controller.abort();
  }, [userId]);

  if (dataUrl) {
    return (
      // The API generates the account's illustration as a data URL.
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={dataUrl}
        alt={t('composer.avatarAlt')}
        title={t('composer.avatarDescription')}
        width={64}
        height={64}
        className="mx-auto mb-6 h-16 w-16 rounded-2xl border border-border object-cover"
      />
    );
  }

  return (
    <div
      aria-hidden="true"
      className="mx-auto mb-6 flex h-16 w-16 items-center justify-center rounded-2xl border border-border bg-surface-muted text-accent"
    >
      <svg
        className="h-6 w-6"
        fill="none"
        viewBox="0 0 24 24"
        stroke="currentColor"
        strokeWidth={1.6}
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="m5 7 5 5-5 5m8 0h6" />
      </svg>
    </div>
  );
}
