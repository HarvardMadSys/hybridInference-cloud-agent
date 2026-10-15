'use client';

import { usePathname, useRouter } from 'next/navigation';
import { useEffect } from 'react';
import toast from 'react-hot-toast';
import { useAuth, useT } from '@/components/providers';

/**
 * Send this tab back to the start when another tab switched the Agents account.
 *
 * The page it was on — a task, an environment, a setting — belonged to the
 * previous account, and the new one may have no such thing. The start page is
 * the new account's own, and the toast says why the tab moved. Until the tab is
 * there the page is not drawn, so it never asks for the previous account's task
 * as the new one.
 *
 * It wraps `ProtectedRoute` rather than sitting inside it: the switch passes
 * through the session's loading state, which unmounts everything under
 * `ProtectedRoute`. `AuthProvider` keeps the switch until this has handled it.
 */
export function SessionSwitchWatcher({ children }: { children: React.ReactNode }) {
  const { sessionSwitched, acknowledgeSessionSwitch } = useAuth();
  const router = useRouter();
  const pathname = usePathname();
  const t = useT();
  const atStart = pathname === '/';

  useEffect(() => {
    if (!sessionSwitched) return;
    if (!atStart) {
      router.replace('/');
      return;
    }
    toast(t('session.switchedElsewhere'));
    acknowledgeSessionSwitch();
  }, [sessionSwitched, atStart, router, t, acknowledgeSessionSwitch]);

  if (sessionSwitched && !atStart) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <div className="h-12 w-12 animate-spin rounded-full border-4 border-gray-300 dark:border-white/15 border-t-blue-600"></div>
      </div>
    );
  }

  return <>{children}</>;
}
