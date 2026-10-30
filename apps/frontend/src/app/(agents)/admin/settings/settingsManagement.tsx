'use client';

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  type ManagementCatalog,
  type ManagementStatus,
  getManagementCatalog,
  getManagementStatus,
} from '@/lib/api/adminManagement';

const STATUS_POLL_MS = 15_000;

export interface SettingsManagement {
  /** Saved, and how many reporting processes use it; a count, never a promise. */
  adoption: string;
  /** Where a field's value comes from and any restart it waits for. */
  fieldNote: (key: string) => string;
}

export const SettingsManagementContext = createContext<SettingsManagement | null>(null);

export function managementView(
  catalog: ManagementCatalog | null,
  status: ManagementStatus | null,
): SettingsManagement | null {
  if (!catalog && !status) return null;
  const { reporting = 0, adopted = 0, saved_revision: revision } = status?.settings ?? {};
  let adoption = '';
  if (status) {
    adoption =
      reporting === 0
        ? `Saved as revision ${revision}. No process reports, so whether it is in use is unknown.`
        : `Saved as revision ${revision}. In use by ${adopted} of ${reporting} reporting processes.`;
  }
  return {
    adoption,
    fieldNote(key) {
      const item = catalog?.items.find((entry) => entry.setting_keys.includes(key));
      const source = item?.sources.map((entry) => entry.meaning).join('; ');
      const waiting = status?.settings.restart_required[key];
      return [
        source ? ` Stored as: ${source}.` : '',
        waiting?.length ? ` Waiting for a restart of: ${waiting.join(', ')}.` : '',
      ].join('');
    },
  };
}

/** What the Admin pages know about saved settings, without assuming a newer control plane. */
export function SettingsManagementProvider({ children }: { children: ReactNode }) {
  const [catalog, setCatalog] = useState<ManagementCatalog | null>(null);
  const [status, setStatus] = useState<ManagementStatus | null>(null);
  useEffect(() => {
    getManagementCatalog().then(setCatalog, () => setCatalog(null));
    // Adoption moves on its own after a save, so it is read again on a timer.
    const read = () => getManagementStatus().then(setStatus, () => setStatus(null));
    void read();
    const timer = window.setInterval(() => void read(), STATUS_POLL_MS);
    return () => window.clearInterval(timer);
  }, []);
  const view = useMemo(() => managementView(catalog, status), [catalog, status]);
  return (
    <SettingsManagementContext.Provider value={view}>{children}</SettingsManagementContext.Provider>
  );
}

/** Whether a saved revision is in use, where the settings are shown. */
export function SettingsAdoption() {
  const view = useContext(SettingsManagementContext);
  return view?.adoption ? <p className="mt-3 text-xs text-muted">{view.adoption}</p> : null;
}
