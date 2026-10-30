'use client';

import { useId, useState } from 'react';

import { Select } from '@/components/ui/Select';
import {
  type DeploymentAgent,
  type DeploymentOverview,
  type ManagementCatalog,
  createEnrollToken,
  createIntent,
  revokeAgent,
} from '@/lib/api/adminManagement';

import { ROLE_NAMES, count, shortVersion, when } from './deploymentCopy';
import {
  BUTTON_CLASS,
  Empty,
  FieldRow,
  GroupCard,
  INPUT_CLASS,
  Pill,
  PRIMARY_CLASS,
  type Run,
  Value,
  useConfirmation,
} from './deploymentUi';
import { MachineConfiguration } from './MachineConfiguration';
import type { Revealed } from './SecurityKeys';

type Role = 'control-plane' | 'host';

export function EnrollMachine({ reveal, run }: { reveal: (secret: Revealed) => void; run: Run }) {
  const id = useId();
  const [role, setRole] = useState<Role>('host');
  const [busy, setBusy] = useState(false);
  return (
    <GroupCard
      title="Enroll a machine"
      icon="link"
      description="Install the deployment agent on the machine as described in its setup guide, which ships with every release, then register it with a token issued here. A token works once, within an hour."
    >
      <FieldRow
        label="Machine role"
        help="A control-plane machine runs the API and web app; a server runs sandboxes."
        labelId={`${id}-role`}
      >
        <div className="flex flex-col gap-2 sm:flex-row">
          <Select
            value={role}
            aria-labelledby={`${id}-role`}
            className={INPUT_CLASS}
            options={[
              { value: 'host', label: 'Server' },
              { value: 'control-plane', label: 'Control plane' },
            ]}
            onValueChange={(next) => setRole(next as Role)}
          />
          <button
            type="button"
            className={`${PRIMARY_CLASS} shrink-0`}
            disabled={busy}
            onClick={() => {
              setBusy(true);
              void run(async () => {
                try {
                  const { token } = await createEnrollToken(role);
                  reveal({
                    title: `Enrollment token for a ${ROLE_NAMES[role].toLowerCase()} machine`,
                    description:
                      'Shown only now. Use it to register the deployment agent within the hour; it works once.',
                    value: token,
                  });
                  return 'Enrollment token issued.';
                } finally {
                  setBusy(false);
                }
              });
            }}
          >
            Issue enrollment token
          </button>
        </div>
      </FieldRow>
    </GroupCard>
  );
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted">{label}</dt>
      <dd className="mt-1 break-words text-sm font-medium text-foreground [overflow-wrap:anywhere]">
        {children}
      </dd>
    </div>
  );
}

/** What the machine reports, at a glance; only what needs explaining gets a row. */
function MachineFacts({ agent }: { agent: DeploymentAgent }) {
  const observed = agent.observed;
  const releases = Object.keys(observed.releases ?? {}).length;
  const unmanaged = (observed.unmanaged_keys ?? []).length;
  const retired = (observed.legacy_names_present ?? []).length;
  return (
    <>
      <dl className="grid gap-x-6 gap-y-4 border-b border-border/60 px-4 py-4 sm:grid-cols-2 sm:px-5 lg:grid-cols-4">
        <Fact label="Running release">
          {observed.deployed_commit ? (
            <span className="font-mono">{shortVersion(observed.deployed_commit)}</span>
          ) : (
            'Not reported'
          )}
        </Fact>
        <Fact label="Configuration version">
          {observed.config_rev != null ? `Version ${observed.config_rev}` : 'None applied yet'}
        </Fact>
        <Fact label="Releases ready">{count(releases, 'release')}</Fact>
        {agent.role === 'host' && (
          <Fact label="Kata runtime">{observed.kata?.version ?? 'Not installed by the agent'}</Fact>
        )}
        <Fact label="Last report">{when(agent.last_report_at) || 'Not yet'}</Fact>
      </dl>
      {unmanaged > 0 && (
        <FieldRow
          label="Set on the machine"
          help="Values configured directly on the machine. Changes made here leave them as they are."
        >
          <Value>{count(unmanaged, 'value')}</Value>
        </FieldRow>
      )}
      {retired > 0 && (
        <FieldRow
          label="Outdated entries"
          help="Entries the machine still has but no longer reads. Remove them on the machine when convenient."
        >
          <Pill tone="warning">{retired === 1 ? '1 entry' : `${retired} entries`}</Pill>
        </FieldRow>
      )}
    </>
  );
}

function MachineActions({ agent, run }: { agent: DeploymentAgent; run: Run }) {
  const id = useId();
  const [version, setVersion] = useState('');
  const [confirm, dialog] = useConfirmation();
  const versions = (agent.observed.config_versions ?? []).filter(
    (candidate) => candidate !== agent.observed.config_rev,
  );
  return (
    <div className="border-t border-border/70">
      {versions.length > 0 && (
        <FieldRow
          label="Restore an earlier configuration"
          help="The machine returns to that version and keeps it only if its services come back healthy."
          labelId={`${id}-version`}
        >
          <div className="flex flex-col gap-2 sm:flex-row">
            <Select
              value={version}
              aria-labelledby={`${id}-version`}
              className={INPUT_CLASS}
              placeholder="Choose a version"
              options={[...versions].reverse().map((candidate) => ({
                value: String(candidate),
                label: `Version ${candidate}`,
              }))}
              onValueChange={setVersion}
            />
            <button
              type="button"
              className={`${BUTTON_CLASS} shrink-0`}
              disabled={!version}
              onClick={() =>
                confirm({
                  title: `Restore version ${version} on ${agent.name}?`,
                  body: (
                    <p>
                      The affected services restart with that configuration. If they do not come
                      back healthy, the machine stays on its current version.
                    </p>
                  ),
                  confirmLabel: 'Restore this version',
                  action: () =>
                    void run(() =>
                      createIntent(agent.agent_id, {
                        operation: 'rollback_config',
                        params: { to_rev: Number(version) },
                        confirm: 'rollback_config',
                      }).then(() => {
                        setVersion('');
                        return `Sent to ${agent.name}.`;
                      }),
                    ),
                })
              }
            >
              Restore
            </button>
          </div>
        </FieldRow>
      )}
      <FieldRow
        label="Revoke deployment agent"
        help="The agent can no longer receive changes. The machine keeps running as it is; enroll it again to manage it here."
      >
        <button
          type="button"
          className={`${BUTTON_CLASS} text-status-error`}
          onClick={() =>
            confirm({
              title: `Revoke the agent on ${agent.name}?`,
              danger: true,
              body: (
                <p>
                  Its credential stops working at once. Nothing on the machine is stopped or
                  changed.
                </p>
              ),
              typed: { label: `Type ${agent.name} to confirm`, expected: agent.name },
              confirmLabel: 'Revoke agent',
              action: (typed) =>
                void run(() => revokeAgent(agent.agent_id, typed).then(() => 'Agent revoked.')),
            })
          }
        >
          Revoke agent
        </button>
      </FieldRow>
      {dialog}
    </div>
  );
}

export function MachineCard({
  agent,
  contract,
  catalog,
  run,
}: {
  agent: DeploymentAgent;
  contract: DeploymentOverview['contract'] | undefined;
  catalog: ManagementCatalog | null;
  run: Run;
}) {
  return (
    <GroupCard
      title={agent.name}
      icon="settings"
      badge={
        <>
          <Pill>{ROLE_NAMES[agent.role] ?? 'Machine'}</Pill>
          <Pill tone={agent.online ? 'success' : 'warning'}>
            {agent.online ? 'Reporting' : 'Not reporting'}
          </Pill>
        </>
      }
    >
      {agent.machine && agent.machine !== agent.name && (
        <FieldRow label="Machine">
          <Value>{agent.machine}</Value>
        </FieldRow>
      )}
      <MachineFacts agent={agent} />
      <MachineConfiguration agent={agent} contract={contract} catalog={catalog} run={run} />
      <MachineActions agent={agent} run={run} />
    </GroupCard>
  );
}

export function Machines({
  agents,
  contract,
  catalog,
  run,
}: {
  agents: DeploymentAgent[];
  contract: DeploymentOverview['contract'] | undefined;
  catalog: ManagementCatalog | null;
  run: Run;
}) {
  if (agents.length === 0) {
    return (
      <GroupCard title="Machines" icon="settings">
        <Empty>
          No machine has a deployment agent yet. Enroll one above; until then, its configuration is
          changed on the machine itself.
        </Empty>
      </GroupCard>
    );
  }
  return (
    <>
      {agents.map((agent) => (
        <MachineCard
          key={agent.agent_id}
          agent={agent}
          contract={contract}
          catalog={catalog}
          run={run}
        />
      ))}
    </>
  );
}
