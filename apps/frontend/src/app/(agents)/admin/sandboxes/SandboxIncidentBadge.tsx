import type { AdminSandbox } from '@/lib/api/adminSandboxes';

/** The sandbox's newest incident within a day, so a sandbox in trouble stands out. */
export function SandboxIncidentBadge({ sandbox }: { sandbox: AdminSandbox }) {
  if (!sandbox.last_incident) return null;
  const kind = sandbox.last_incident.replace(/[._]+/g, ' ');
  return (
    <span
      className="mt-1 block truncate text-xs text-red-700 dark:text-red-400"
      title={sandbox.last_incident_at ?? undefined}
    >
      Incident: {kind}
    </span>
  );
}
