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

import { readable } from './deploymentCopy';
import {
  BUTTON_CLASS,
  type Confirmation,
  Empty,
  FieldRow,
  GroupCard,
  Pill,
  type Run,
  type Tone,
  useConfirmation,
} from './deploymentUi';
import { isActive } from './workflowSteps';

export interface Revealed {
  title: string;
  description: string;
  value: string;
}

interface KeyRow {
  id: string;
  label: string;
  protects: string;
  /** Which workflow kind shows this row as running. */
  kind: string;
  /** For kinds that rotate one of several keys, which one this row is. */
  key?: string;
  /** Why it cannot start now; nothing when it can. */
  blocked?: string;
  warning: string;
  start: () => Promise<string | void>;
}

function rows(
  controlPlane: DeploymentAgent[],
  hosts: DeploymentAgent[],
  enrolled: number,
  reveal: (secret: Revealed) => void,
): KeyRow[] {
  const noControlPlane = controlPlane.length
    ? undefined
    : 'Needs a deployment agent on a control-plane machine.';
  const noServer = enrolled ? undefined : 'No server is enrolled yet.';
  const credentialKey = (id: string, key: string, label: string, protects: string): KeyRow => ({
    id,
    label,
    protects,
    kind: 'credential_key',
    key,
    warning:
      'Every stored credential is re-encrypted with a new key. Both keys keep working until that finishes, so nobody is signed out.',
    start: () =>
      startWorkflow({ kind: 'credential_key', params: { key }, secret: randomSecret() }).then(
        () => undefined,
      ),
  });
  return [
    {
      id: 'admin_token',
      label: 'Recovery administrator token',
      protects: 'Full administrator access for when normal sign-in is unavailable.',
      kind: 'admin_token',
      blocked: noControlPlane,
      warning:
        'A new token is generated in this browser and replaces the current one on every control-plane machine. It is shown to you once, so have a safe place ready to store it.',
      start: async () => {
        const token = randomSecret();
        await startWorkflow({
          kind: 'admin_token',
          params: { token_id: await keyIdentifier(token) },
          sealed_by_agent: await sealForAgents(controlPlane, 'ADMIN_TOKEN', token),
        });
        reveal({
          title: 'New recovery administrator token',
          description:
            'Shown only now. Store it with your recovery material; the current token stops working once every machine has the new one.',
          value: token,
        });
        return 'Rotation started. Its progress is shown below.';
      },
    },
    {
      id: 'root_key',
      label: 'Settings root key',
      protects: 'Saved settings secrets and the sandbox archive key.',
      kind: 'root_key',
      blocked: noControlPlane,
      warning:
        'Saved secrets are re-encrypted with a new key before the old key is dropped. The new key is generated in this browser and never shown.',
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
      label: 'Server signing key',
      protects: 'Server credentials and the tokens of running turns.',
      kind: 'control_signing',
      warning:
        'Every server renews its credential at its next check-in. The previous key is retired only after that, and after turns that started earlier finish.',
      start: () =>
        startWorkflow({ kind: 'control_signing', secret: randomSecret() }).then(() => undefined),
    },
    {
      id: 'sandbox_manager_token',
      label: 'Sandbox manager credential',
      protects: 'What the control plane presents to each server.',
      kind: 'sandbox_manager_token',
      blocked: noServer,
      warning:
        'Servers change over one at a time, each finishing its running turns first. Both credentials are accepted during the change.',
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
      label: 'Tunnel keys',
      protects: 'The keys each server’s tunnels sign in with.',
      kind: 'tunnel_keys',
      blocked: noServer,
      warning:
        'Servers change over one at a time, each finishing its running turns first. If a tunnel ends somewhere this deployment cannot update, you will be asked to add the new keys there yourself.',
      start: () => startWorkflow({ kind: 'tunnel_keys' }).then(() => undefined),
    },
  ];
}

function credentialState(host: HostCredentialView): [string, Tone] {
  if (host.current_key) return ['Up to date', 'success'];
  const lag = readable(host.lag);
  if (host.status === 'offline') return [lag || 'Offline', 'warning'];
  if (lag) return [lag, 'warning'];
  return host.key_known
    ? ['Issued with the previous key', 'warning']
    : ['Not known yet', 'neutral'];
}

/** Which signing key issued each server's credential, and a fresh one on request. */
function ServerCredentials({ hosts, run }: { hosts: HostCredentialView[]; run: Run }) {
  const [confirm, dialog] = useConfirmation();
  return (
    <GroupCard
      title="Server credentials"
      icon="lock"
      description="Each server signs in with a credential issued by the signing key. A server renews it at its next check-in."
    >
      {hosts.length === 0 ? (
        <Empty>No server has reported a credential yet.</Empty>
      ) : (
        hosts.map((host) => {
          const [state, tone] = credentialState(host);
          return (
            <FieldRow key={host.host} label={host.host}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <Pill tone={tone}>{state}</Pill>
                {!host.renews ? (
                  <span className="text-xs text-muted">Can’t renew by itself; enroll it again</span>
                ) : host.renewal_pending || host.renewal_requested ? (
                  <Pill tone="accent">Renewal requested</Pill>
                ) : (
                  <button
                    type="button"
                    className={BUTTON_CLASS}
                    aria-label={`Renew ${host.host}`}
                    onClick={() =>
                      confirm({
                        title: `Renew the credential of ${host.host}?`,
                        body: (
                          <p>
                            The server receives a new credential at its next check-in. The old one
                            stops working shortly after.
                          </p>
                        ),
                        confirmLabel: 'Renew',
                        action: () =>
                          void run(() =>
                            renewHostCredential(host.host).then(
                              () => `${host.host} renews its credential at its next check-in.`,
                            ),
                          ),
                      })
                    }
                  >
                    Renew
                  </button>
                )}
              </div>
            </FieldRow>
          );
        })
      )}
      {dialog}
    </GroupCard>
  );
}

/** One key: what it protects, whether it is rotating now, and its rotation. */
function KeyRowView({
  row,
  active,
  confirm,
  run,
}: {
  row: KeyRow;
  active: DeploymentWorkflow[];
  confirm: (ask: Confirmation) => void;
  run: Run;
}) {
  const sameKind = active.filter((workflow) => workflow.kind === row.kind);
  const rotating = sameKind.some(
    (workflow) => !row.key || !workflow.params?.key || workflow.params.key === row.key,
  );
  // Only one rotation of a kind runs at a time; the other key waits for it.
  const waiting = !rotating && sameKind.length > 0;
  return (
    <FieldRow label={row.label} help={row.protects}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        {rotating ? (
          <Pill tone="accent">Rotating</Pill>
        ) : waiting || row.blocked ? (
          <span className="text-xs leading-5 text-muted">
            {waiting ? 'Available when the other credential key finishes.' : row.blocked}
          </span>
        ) : (
          <span />
        )}
        <button
          type="button"
          className={BUTTON_CLASS}
          disabled={Boolean(row.blocked) || rotating || waiting}
          aria-label={`Rotate ${row.label}`}
          onClick={() =>
            confirm({
              title: `Rotate the ${row.label.charAt(0).toLowerCase()}${row.label.slice(1)}?`,
              body: <p>{row.warning}</p>,
              confirmLabel: 'Start rotation',
              action: () => void run(row.start),
            })
          }
        >
          {rotating ? 'Rotating…' : 'Rotate'}
        </button>
      </div>
    </FieldRow>
  );
}

/** Every key and credential, what it protects, and the rotation that replaces it. */
export function SecurityKeys({
  controlPlane,
  hosts,
  workflows,
  run,
  reveal,
}: {
  controlPlane: DeploymentAgent[];
  hosts: DeploymentAgent[];
  workflows: DeploymentWorkflow[];
  run: Run;
  reveal: (secret: Revealed) => void;
}) {
  const [servers, setServers] = useState<HostCredentialView[]>([]);
  const [confirm, dialog] = useConfirmation();
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
  const active = workflows.filter(isActive);
  return (
    <>
      <GroupCard
        title="Keys"
        icon="lock"
        description="A rotation keeps the old key working until every machine and server has moved to the new one. New keys are generated in your browser and are never shown, except the recovery token, once."
      >
        {rows(controlPlane, hosts, servers.length, reveal).map((row) => (
          <KeyRowView key={row.id} row={row} active={active} confirm={confirm} run={run} />
        ))}
        {dialog}
      </GroupCard>
      <ServerCredentials hosts={servers} run={run} />
    </>
  );
}
