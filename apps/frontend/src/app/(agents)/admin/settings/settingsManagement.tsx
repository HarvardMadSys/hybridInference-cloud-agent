'use client';

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import {
  type ManagementCatalog,
  type ManagementStatus,
  getManagementCatalog,
  getManagementStatus,
} from '@/lib/api/adminManagement';

import { listing, processName } from '../deployment/deploymentCopy';

const STATUS_POLL_MS = 15_000;

export interface SettingsManagement {
  /** Saved, and how many running processes use it; a count, never a promise. */
  adoption: string;
  /** Any restart a field's saved value still waits for. */
  fieldNote: (key: string) => string;
}

export const SettingsManagementContext = createContext<SettingsManagement | null>(null);

function inUse(reporting: number, adopted: number): string {
  if (reporting === 0) {
    return 'Saved. No running process has reported yet, so whether these settings are in use is unknown.';
  }
  if (adopted === reporting) {
    return reporting === 1
      ? 'Saved. The one running process uses these settings.'
      : `Saved. All ${reporting} running processes use these settings.`;
  }
  return `Saved. ${adopted} of ${reporting} running processes use these settings so far.`;
}

export function managementView(
  catalog: ManagementCatalog | null,
  status: ManagementStatus | null,
): SettingsManagement | null {
  if (!catalog && !status) return null;
  const { reporting = 0, adopted = 0 } = status?.settings ?? {};
  return {
    adoption: status ? inUse(reporting, adopted) : '',
    fieldNote(key) {
      const waiting = status?.settings.restart_required[key];
      if (!waiting?.length) return '';
      return ` Still waiting for a restart of ${listing(waiting.map(processName))}.`;
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
