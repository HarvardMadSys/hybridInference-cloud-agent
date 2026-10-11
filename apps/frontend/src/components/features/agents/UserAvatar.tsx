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
        width={80}
        height={80}
        className="mx-auto mb-5 h-20 w-20 rounded-full border border-border object-cover"
      />
    );
  }

  return (
    <div
      aria-hidden="true"
      className="mx-auto mb-5 flex h-20 w-20 items-center justify-center rounded-full border border-crimson/10 bg-crimson/[0.06] text-crimson shadow-[0_4px_16px_-8px_rgba(165,28,48,0.3)] dark:border-crimson-light/20 dark:bg-crimson/10 dark:text-crimson-light"
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
