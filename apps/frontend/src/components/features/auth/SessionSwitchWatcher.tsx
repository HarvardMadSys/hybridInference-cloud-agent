'use client';

import { useRouter } from 'next/navigation';
import { useEffect } from 'react';
import toast from 'react-hot-toast';
import { useT } from '@/components/providers';
import { SESSION_SWITCHED_EVENT } from '@/lib/auth/sessionSync';

/**
 * Send this tab back to the start when another tab switched the Agents account.
 *
 * The page it was on — a task, an environment, a setting — belonged to the
 * previous account, and the new one may have no such thing. The start page is
 * the new account's own, and the toast says why the tab moved.
 */
export function SessionSwitchWatcher() {
  const router = useRouter();
  const t = useT();

  useEffect(() => {
    const onSwitched = () => {
      toast(t('session.switchedElsewhere'));
      router.replace('/');
    };
    window.addEventListener(SESSION_SWITCHED_EVENT, onSwitched);
    return () => window.removeEventListener(SESSION_SWITCHED_EVENT, onSwitched);
  }, [router, t]);

  return null;
}
