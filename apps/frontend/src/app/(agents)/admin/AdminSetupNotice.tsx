'use client';

import Link from 'next/link';
import { useEffect, useState } from 'react';

import { getAgentAdminSettings, type AgentAdminSettings } from '@/lib/api/adminSettings';

export function AdminSetupNotice() {
  const [setup, setSetup] = useState<AgentAdminSettings['setup']>();

  useEffect(() => {
    const controller = new AbortController();
    void getAgentAdminSettings(controller.signal)
      .then((snapshot) => {
        if (!controller.signal.aborted) setSetup(snapshot.setup);
      })
      .catch(() => {
        // Settings details remain behind the administrator guard. A failed read
        // gives no basis to show an incomplete-configuration notification.
      });
    return () => controller.abort();
  }, []);

  if (!setup?.required) return null;
  const features = setup.groups.filter((group) => group.needs_setup).map((group) => group.label);
  return (
    <div
      role="status"
      className="mt-5 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950/30 dark:text-amber-100"
    >
      Administrator setup required{features.length > 0 ? `: ${features.join(', ')}.` : '.'}{' '}
      <Link href="/admin/settings" className="font-medium underline">
        Complete missing settings
      </Link>
    </div>
  );
}
