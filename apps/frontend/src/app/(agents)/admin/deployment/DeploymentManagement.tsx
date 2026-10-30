'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  type AdminCredential,
  type DeploymentAgent,
  type DeploymentOverview,
  type ManagementCatalog,
  type ManagementStatus,
  cancelIntent,
  createCredential,
  createEnrollToken,
  createIntent,
  getDeployments,
  getManagementCatalog,
  getManagementStatus,
  listCredentials,
  revokeAgent,
  revokeCredential,
  startRollout,
} from '@/lib/api/adminManagement';
import { ConfigurationMap } from './ConfigurationMap';
import { DatabaseOperations, MaintenancePanel } from './DatabaseOperations';
import { MachineConfiguration } from './MachineConfiguration';
import { SecurityKeys } from './SecurityKeys';
import { WorkflowProgress } from './WorkflowProgress';

const BUTTON =
  'rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-surface-hover disabled:opacity-50';
const INPUT = 'rounded-md border border-border bg-surface px-2 py-1 text-sm text-foreground';
const SECTION = 'mt-6 rounded-lg border border-border p-4';

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function DeploymentManagement() {
  const [overview, setOverview] = useState<DeploymentOverview | null>(null);
  const [status, setStatus] = useState<ManagementStatus | null>(null);
  const [credentials, setCredentials] = useState<AdminCredential[]>([]);
  const [catalog, setCatalog] = useState<ManagementCatalog | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  useEffect(() => {
    // The catalog never changes while the page is open, and an earlier
    // control plane has none: the page then simply goes without the map.
    getManagementCatalog().then(setCatalog, () => setCatalog(null));
  }, []);

  const refresh = useCallback(async () => {
    try {
      const [deployments, management, creds] = await Promise.all([
        getDeployments(),
        getManagementStatus(),
        listCredentials(),
      ]);
      setOverview(deployments);
      setStatus(management);
      setCredentials(creds.credentials);
      setError('');
    } catch (caught) {
      setError(`The control plane is not answering; it may be restarting. ${message(caught)}`);
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const run = useCallback(
    async (work: () => Promise<string | void>) => {
      try {
        const done = await work();
        setNotice(done ?? 'Requested. The state below updates as the agents report.');
        setError('');
        await refresh();
      } catch (caught) {
        setError(message(caught));
      }
    },
    [refresh],
  );

  const agents = useMemo(() => (overview?.agents ?? []).filter((a) => !a.revoked), [overview]);
  const controlPlane = agents.filter((a) => a.role === 'control-plane');
  const hosts = agents.filter((a) => a.role === 'host');

  return (
    <div>
      {error && (
        <p role="alert" className="mt-4 rounded-md bg-red-50 px-3 py-2 text-sm text-red-800">
          {error}
        </p>
      )}
      {notice && (
        <p role="status" className="mt-4 rounded-md bg-surface px-3 py-2 text-sm text-foreground">
          {notice}
        </p>
      )}
      <MaintenancePanel maintenance={overview?.maintenance} />
      <AdoptionSummary status={status} />
      <EnrollMachine onIssued={(token) => setNotice(`Enrollment token (shown once): ${token}`)} />
      <section className={SECTION} aria-labelledby="machines">
        <h2 id="machines" className="text-base font-semibold">
          Machines
        </h2>
        {agents.length === 0 ? (
          <p className="mt-2 text-sm text-muted">
            No machine has a deployment agent. Enroll one above; until then, these machines are
            deployed by hand.
          </p>
        ) : (
          agents.map((agent) => (
            <AgentPanel
              key={agent.agent_id}
              agent={agent}
              contract={overview?.contract}
              catalog={catalog}
              run={run}
            />
          ))
        )}
      </section>
      <Releases agents={agents} run={run} />
      <SecurityKeys
        controlPlane={controlPlane}
        hosts={hosts}
        workflows={overview?.workflows ?? []}
        run={run}
      />
      <DatabaseOperations
        controlPlane={controlPlane}
        workflows={overview?.workflows ?? []}
        run={run}
      />
      <Operations overview={overview} run={run} />
      <Credentials credentials={credentials} run={run} onCreated={setNotice} />
      <ConfigurationMap catalog={catalog} />
    </div>
  );
}

function AdoptionSummary({ status }: { status: ManagementStatus | null }) {
  if (!status) return null;
  const restart = Object.entries(status.settings.restart_required);
  return (
    <section className={SECTION} aria-labelledby="adoption">
      <h2 id="adoption" className="text-base font-semibold">
        Saved and adopted
      </h2>
      <p className="mt-2 text-sm">
        Settings revision {status.settings.saved_revision}: adopted by {status.settings.adopted} of{' '}
        {status.settings.reporting} reporting control-plane processes.
      </p>
      {restart.length > 0 && (
        <p className="mt-1 text-sm">
          Waiting for a restart:{' '}
          {restart.map(([key, where]) => `${key} (${where.join(', ')})`).join('; ')}
        </p>
      )}
      {status.unidentified_sessions.length > 0 && (
        <p className="mt-1 text-sm">
          {status.unidentified_sessions.length} database session(s) were not opened by a reporting
          process (an older release or a tool):{' '}
          {status.unidentified_sessions
            .map((s) => s.application_name || s.client_addr || 'unnamed')
            .join(', ')}
          . Their state is unknown.
        </p>
      )}
      <ul className="mt-2 text-xs text-muted">
        {status.instances.map((instance) => (
          <li key={instance.instance_id}>
            {instance.role}@{instance.machine} {instance.build_sha.slice(0, 10) || 'unknown build'}{' '}
            —{' '}
            {instance.current
              ? instance.adopted
                ? 'adopted'
                : `revision ${instance.adopted_settings_revision}`
              : instance.stopped
                ? 'stopped'
                : 'not reporting'}
          </li>
        ))}
      </ul>
    </section>
  );
}

function EnrollMachine({ onIssued }: { onIssued: (token: string) => void }) {
  const [role, setRole] = useState<'control-plane' | 'host'>('host');
  const [busy, setBusy] = useState(false);
  return (
    <section className={SECTION} aria-labelledby="enroll">
      <h2 id="enroll" className="text-base font-semibold">
        Enroll a machine
      </h2>
      <p className="mt-1 text-sm text-muted">
        On the machine: <code>python -m deploy.agent --root DEPLOY_ROOT init</code>, then{' '}
        <code>register --token</code> with the token issued here (valid for one hour, once).
      </p>
      <div className="mt-2 flex gap-2">
        <select
          className={INPUT}
          value={role}
          onChange={(e) => setRole(e.target.value as typeof role)}
          aria-label="Machine role"
        >
          <option value="host">Host</option>
          <option value="control-plane">Control plane</option>
        </select>
        <button
          type="button"
          className={BUTTON}
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            try {
              onIssued((await createEnrollToken(role)).token);
            } finally {
              setBusy(false);
            }
          }}
        >
          Issue enrollment token
        </button>
      </div>
    </section>
  );
}

type Run = (work: () => Promise<string | void>) => Promise<void>;

function AgentPanel({
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
  const [rev, setRev] = useState('');
  const observed = agent.observed;
  return (
    <div className="mt-3 rounded-md border border-border p-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <strong>{agent.name}</strong>
        <span className="text-xs text-muted">
          {agent.role} · {agent.machine} · {agent.online ? 'reporting' : 'not reporting'}
        </span>
      </div>
      <ObservedFacts agent={agent} />
      <MachineConfiguration agent={agent} contract={contract} catalog={catalog} run={run} />
      <div className="mt-2 flex flex-wrap gap-2">
        <select
          className={INPUT}
          value={rev}
          onChange={(e) => setRev(e.target.value)}
          aria-label="Configuration version"
        >
          <option value="">Roll back to…</option>
          {(observed.config_versions ?? []).map((version) => (
            <option key={version} value={version}>
              version {version}
            </option>
          ))}
        </select>
        <button
          type="button"
          className={BUTTON}
          disabled={!rev}
          onClick={() =>
            void run(() =>
              createIntent(agent.agent_id, {
                operation: 'rollback_config',
                params: { to_rev: Number(rev) },
                confirm: 'rollback_config',
              }).then(() => undefined),
            )
          }
        >
          Roll back
        </button>
        <button
          type="button"
          className={BUTTON}
          onClick={() => {
            const typed = window.prompt(`Type ${agent.name} to revoke this agent's credential`);
            if (typed)
              void run(() => revokeAgent(agent.agent_id, typed).then(() => 'Agent revoked.'));
          }}
        >
          Revoke agent
        </button>
      </div>
    </div>
  );
}

function Releases({ agents, run }: { agents: DeploymentAgent[]; run: Run }) {
  const shared = useMemo(() => {
    if (agents.length === 0) return [];
    const [first, ...rest] = agents;
    return Object.entries(first.observed.releases ?? {}).filter(([sha]) =>
      rest.every((agent) => sha in (agent.observed.releases ?? {})),
    );
  }, [agents]);
  return (
    <section className={SECTION} aria-labelledby="releases">
      <h2 id="releases" className="text-base font-semibold">
        Desired release
      </h2>
      <p className="mt-1 text-sm text-muted">
        Releases every machine has imported with a pinned digest. Deploying prepares every machine,
        migrates, activates the hosts and then the control plane.
      </p>
      {shared.length === 0 ? (
        <p className="mt-2 text-sm text-muted">No release is imported on every machine.</p>
      ) : (
        <ul className="mt-2 text-sm">
          {shared.map(([sha, entry]) => (
            <li key={sha} className="flex flex-wrap items-center gap-2">
              <code>{entry.source_commit.slice(0, 12)}</code>
              <span className="text-xs text-muted">{entry.database_revision}</span>
              <button
                type="button"
                className={BUTTON}
                onClick={() => {
                  if (
                    window.confirm(`Deploy ${entry.source_commit.slice(0, 12)} to every machine?`)
                  ) {
                    void run(() => startRollout(sha).then(() => undefined));
                  }
                }}
              >
                Deploy
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Operations({ overview, run }: { overview: DeploymentOverview | null; run: Run }) {
  const workflows = overview?.workflows ?? [];
  return (
    <section className={SECTION} aria-labelledby="operations">
      <h2 id="operations" className="text-base font-semibold">
        Operations
      </h2>
      {workflows.length === 0 ? (
        <p className="mt-2 text-sm text-muted">No operation has run yet.</p>
      ) : (
        <ul>
          {workflows.map((workflow) => (
            <WorkflowProgress key={workflow.workflow_id} workflow={workflow} run={run} />
          ))}
        </ul>
      )}
      <h3 className="mt-4 text-sm font-semibold">Recent agent steps</h3>
      <ul className="mt-1 text-xs">
        {(overview?.intents ?? []).slice(0, 20).map((intent) => (
          <li key={intent.intent_id}>
            {intent.operation} — {intent.state}
            {intent.detail ? `: ${intent.detail}` : ''}
            {intent.state === 'pending' && (
              <button
                type="button"
                className="ml-2 underline"
                onClick={() => void run(() => cancelIntent(intent.intent_id).then(() => undefined))}
              >
                Cancel
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Credentials({
  credentials,
  run,
  onCreated,
}: {
  credentials: AdminCredential[];
  run: Run;
  onCreated: (text: string) => void;
}) {
  const [name, setName] = useState('');
  const [deployment, setDeployment] = useState(false);
  return (
    <section className={SECTION} aria-labelledby="credentials">
      <h2 id="credentials" className="text-base font-semibold">
        Administrator API credentials
      </h2>
      <p className="mt-1 text-sm text-muted">
        Named and revocable, for automation. They cannot create credentials, and act on deployments
        only with the deployment scope. ADMIN_TOKEN is separate and rotated above.
      </p>
      <form
        className="mt-2 flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void run(async () => {
            const created = await createCredential(name, deployment, 90);
            onCreated(`Credential ${created.name} (shown once): ${created.credential}`);
            setName('');
            return `Credential ${created.name} (shown once): ${created.credential}`;
          });
        }}
      >
        <input
          className={INPUT}
          value={name}
          onChange={(e) => setName(e.target.value)}
          aria-label="Credential name"
          placeholder="Name"
        />
        <label className="text-xs">
          <input
            type="checkbox"
            checked={deployment}
            onChange={(e) => setDeployment(e.target.checked)}
          />{' '}
          Deployment scope
        </label>
        <button type="submit" className={BUTTON} disabled={!name}>
          Create
        </button>
      </form>
      <ul className="mt-2 text-sm">
        {credentials.map((credential) => (
          <li key={credential.credential_id}>
            {credential.name} ({credential.scopes.join(', ')}) —{' '}
            {credential.revoked_at ? 'revoked' : `last used ${credential.last_used_at ?? 'never'}`}
            {!credential.revoked_at && (
              <button
                type="button"
                className="ml-2 underline"
                onClick={() =>
                  void run(() =>
                    revokeCredential(credential.credential_id).then(() => 'Credential revoked.'),
                  )
                }
              >
                Revoke
              </button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

function ObservedFacts({ agent }: { agent: DeploymentAgent }) {
  const observed = agent.observed;
  return (
    <dl className="mt-2 grid grid-cols-1 gap-x-4 text-xs sm:grid-cols-2">
      <dt className="text-muted">Running commit</dt>
      <dd>{observed.deployed_commit || 'unknown'}</dd>
      <dt className="text-muted">Configuration version</dt>
      <dd>{observed.config_rev ?? 'none yet'}</dd>
      <dt className="text-muted">Verified releases</dt>
      <dd>{Object.keys(observed.releases ?? {}).length}</dd>
      {agent.role === 'host' && (
        <>
          <dt className="text-muted">Kata</dt>
          <dd>{observed.kata ? observed.kata.version : 'not installed by the agent'}</dd>
        </>
      )}
      <dt className="text-muted">Lines it does not manage</dt>
      <dd>{(observed.unmanaged_keys ?? []).length}</dd>
      {(observed.legacy_names_present ?? []).length > 0 && (
        <>
          <dt className="text-muted">Retired names still set</dt>
          <dd>{(observed.legacy_names_present ?? []).join(', ')}</dd>
        </>
      )}
    </dl>
  );
}
