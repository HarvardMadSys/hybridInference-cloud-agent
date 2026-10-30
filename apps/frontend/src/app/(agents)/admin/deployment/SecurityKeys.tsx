'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  type DeploymentAgent,
  type DeploymentWorkflow,
  type HostCredentialView,
  getHostCredentials,
  keyIdentifier,
  randomSecret,
  renewHostCredential,
  sealForAgents,
  startWorkflow,
} from '@/lib/api/adminManagement';
import { isActive } from './workflowSteps';

type Run = (work: () => Promise<string | void>) => Promise<void>;

const BUTTON =
  'rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-surface-hover disabled:opacity-50';
const SECTION = 'mt-6 rounded-lg border border-border p-4';

interface KeyRow {
  id: string;
  label: string;
  protects: string;
  /** Which workflow kind shows this row as running. */
  kind: string;
  /** Why it cannot start now; nothing when it can. */
  blocked?: string;
  warning: string;
  start: () => Promise<string | void>;
}

function rows(
  controlPlane: DeploymentAgent[],
  hosts: DeploymentAgent[],
  enrolled: number,
): KeyRow[] {
  const noControlPlane = controlPlane.length
    ? undefined
    : 'Needs a deployment agent on the control-plane machine.';
  const credentialKey = (id: string, key: string, label: string, protects: string): KeyRow => ({
    id,
    label,
    protects,
    kind: 'credential_key',
    warning: `Rotate the ${label.toLowerCase()}? Every stored value is re-encrypted first; both keys keep working until that finishes.`,
    start: () =>
      startWorkflow({ kind: 'credential_key', params: { key }, secret: randomSecret() }).then(
        () => undefined,
      ),
  });
  return [
    {
      id: 'admin_token',
      label: 'ADMIN_TOKEN',
      protects: 'Full administrator access for recovery.',
      kind: 'admin_token',
      blocked: noControlPlane,
      warning: 'Rotate ADMIN_TOKEN? The new value is shown once.',
      start: async () => {
        const token = randomSecret();
        await startWorkflow({
          kind: 'admin_token',
          params: { token_id: await keyIdentifier(token) },
          sealed_by_agent: await sealForAgents(controlPlane, 'ADMIN_TOKEN', token),
        });
        return `New ADMIN_TOKEN (shown once, store it with your recovery material): ${token}`;
      },
    },
    {
      id: 'root_key',
      label: 'Settings root key',
      protects: 'Saved settings secrets and the sandbox archive key.',
      kind: 'root_key',
      blocked: noControlPlane,
      warning:
        'Rotate the settings root key? Saved secrets are re-encrypted before the old key is dropped.',
      start: async () => {
        const key = randomSecret();
        await startWorkflow({
          kind: 'root_key',
          params: { new_key_id: await keyIdentifier(key) },
          sealed_by_agent: await sealForAgents(controlPlane, 'AGENT_SETTINGS_ENCRYPTION_KEY', key),
        });
      },
    },
    credentialKey(
      'user_key',
      'user',
      'User credential key',
      'Source-control, MCP, environment, SSH and model connection credentials.',
    ),
    credentialKey(
      'compute_key',
      'compute',
      'Compute credential key',
      'Compute connections and deployment profile inputs.',
    ),
    {
      id: 'control_signing',
      label: 'Host and worker signing key',
      protects: 'Server credentials and the tokens of running turns.',
      kind: 'control_signing',
      warning:
        'Rotate the signing key? Every server renews its credential at its next heartbeat, and the previous key is retired only after that and after earlier turns finish.',
      start: () =>
        startWorkflow({ kind: 'control_signing', secret: randomSecret() }).then(() => undefined),
    },
    {
      id: 'sandbox_manager_token',
      label: 'Sandbox manager credential',
      protects: 'What the control plane presents to each server.',
      kind: 'sandbox_manager_token',
      blocked: enrolled ? undefined : 'No server is enrolled.',
      warning:
        'Rotate the sandbox manager credential? Servers change one at a time, each draining its running turns first.',
      start: async () => {
        const token = randomSecret();
        await startWorkflow({
          kind: 'sandbox_manager_token',
          sealed_by_agent: await sealForAgents(hosts, 'AGENT_SANDBOX_MANAGER_TOKEN_NEXT', token),
          secret: token,
        });
      },
    },
    {
      id: 'tunnel_keys',
      label: 'Tunnel SSH keys',
      protects: 'The three tunnels each server opens.',
      kind: 'tunnel_keys',
      blocked: enrolled ? undefined : 'No server is enrolled.',
      warning:
        'Rotate the tunnel keys of every server? Servers change one at a time, each draining first. You may be asked to change a far end this deployment cannot reach.',
      start: () => startWorkflow({ kind: 'tunnel_keys' }).then(() => undefined),
    },
  ];
}

function credentialState(host: HostCredentialView): string {
  if (host.current_key) return 'Current key';
  if (host.lag) return host.lag;
  return host.key_known ? 'Earlier key' : 'Key not known yet';
}

/** Which signing key minted each server's credential, and a fresh one on request. */
function ServerCredentials({ hosts, run }: { hosts: HostCredentialView[]; run: Run }) {
  if (hosts.length === 0) return null;
  return (
    <div className="mt-4">
      <h3 className="text-sm font-semibold">Server credentials</h3>
      <table className="mt-1 w-full text-left text-xs">
        <thead>
          <tr className="text-muted">
            <th className="py-1 font-medium">Server</th>
            <th className="py-1 font-medium">Credential</th>
            <th className="py-1 font-medium">Renewal</th>
          </tr>
        </thead>
        <tbody>
          {hosts.map((host) => (
            <tr key={host.host} className="border-t border-border">
              <td className="py-1 pr-3">{host.host}</td>
              <td className="py-1 pr-3">{credentialState(host)}</td>
              <td className="py-1">
                {!host.renews ? (
                  <span className="text-muted">Re-enroll: this runner cannot renew</span>
                ) : host.renewal_pending || host.renewal_requested ? (
                  <span className="text-muted">Requested</span>
                ) : (
                  <button
                    type="button"
                    className="underline"
                    onClick={() => {
                      if (
                        window.confirm(
                          `Give ${host.host} a new credential at its next heartbeat? The old one stops working shortly after.`,
                        )
                      ) {
                        void run(() => renewHostCredential(host.host).then(() => undefined));
                      }
                    }}
                  >
                    Renew
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Every key and credential, what it protects, and the rotation that replaces it. */
export function SecurityKeys({
  controlPlane,
  hosts,
  workflows,
  run,
}: {
  controlPlane: DeploymentAgent[];
  hosts: DeploymentAgent[];
  workflows: DeploymentWorkflow[];
  run: Run;
}) {
  const [servers, setServers] = useState<HostCredentialView[]>([]);
  const load = useCallback(async () => {
    try {
      setServers((await getHostCredentials()).hosts);
    } catch {
      setServers([]); // an earlier control plane has no such report
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load, workflows]);
  const running = new Set(workflows.filter(isActive).map((workflow) => workflow.kind));
  return (
    <section className={SECTION} aria-labelledby="keys">
      <h2 id="keys" className="text-base font-semibold">
        Keys and credentials
      </h2>
      <p className="mt-1 text-sm text-muted">
        Each rotation keeps the old material working until every process and server has moved. New
        values are generated in this browser and never shown, except ADMIN_TOKEN, once.
      </p>
      <table className="mt-2 w-full text-left text-sm">
        <tbody>
          {rows(controlPlane, hosts, servers.length).map((row) => (
            <tr key={row.id} className="border-t border-border align-top">
              <td className="py-2 pr-3">
                <div className="font-medium">{row.label}</div>
                <div className="text-xs text-muted">{row.protects}</div>
                {row.blocked && <div className="text-xs text-muted">{row.blocked}</div>}
              </td>
              <td className="py-2 text-right">
                <button
                  type="button"
                  className={BUTTON}
                  disabled={Boolean(row.blocked) || running.has(row.kind)}
                  aria-label={`Rotate ${row.label}`}
                  onClick={() => {
                    if (window.confirm(row.warning)) void run(row.start);
                  }}
                >
                  {running.has(row.kind) ? 'Running' : 'Rotate'}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ServerCredentials hosts={servers} run={run} />
    </section>
  );
}
