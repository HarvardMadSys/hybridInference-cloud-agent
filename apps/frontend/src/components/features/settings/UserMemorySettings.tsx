'use client';

import { useAuth, useLocale } from '@/components/providers';
import { MemoryScopePanel } from './MemoryScopePanel';

/** Memory that applies to every project. */
export function UserMemorySettings() {
  const { state } = useAuth();
  const { t } = useLocale();
  const userId = state.user?.id;

  return (
    <>
      <p className="text-xs leading-relaxed text-muted">{t('settings.memory.hint')}</p>
      {userId ? (
        // A new account gets a fresh panel immediately, including before effects run.
        <MemoryScopePanel key={`user:${userId}`} repo={null} />
      ) : (
        <p className="mt-3 text-sm text-muted">{t('settings.memory.signIn')}</p>
      )}
    </>
  );
}
