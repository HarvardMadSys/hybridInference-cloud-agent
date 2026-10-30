'use client';

import { useEffect, useState } from 'react';

import { config } from '@/config/env';

const POLL_MS = 30_000;

/**
 * Says why changes are being refused while the database moves.
 *
 * Read from the public site configuration, so it works for everyone and needs
 * nothing of a session; a failed read, including from the control plane that
 * restarts during the move, leaves the last answer on screen.
 */
export function MaintenanceBanner() {
  const [active, setActive] = useState(false);
  useEffect(() => {
    let stopped = false;
    const read = async () => {
      try {
        const response = await fetch(`${config.apiBase}/v1/agent/site-config`);
        if (!response.ok) return;
        const body = (await response.json()) as { maintenance?: { active?: boolean } };
        if (!stopped) setActive(body.maintenance?.active === true);
      } catch {
        // Unreachable says nothing about maintenance.
      }
    };
    void read();
    const timer = window.setInterval(() => void read(), POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, []);
  if (!active) return null;
  return (
    <div
      role="status"
      className="bg-amber-100 px-3 py-1.5 text-center text-sm text-amber-900 dark:bg-amber-500/15 dark:text-amber-200"
    >
      Maintenance: the database is moving. You can read everything, but changes are refused until it
      finishes, and the service restarts briefly at the end.
    </div>
  );
}
