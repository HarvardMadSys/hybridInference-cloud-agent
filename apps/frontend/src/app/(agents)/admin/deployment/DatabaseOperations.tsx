'use client';

import { useState } from 'react';
import {
  type DatabaseAddress,
  type DeploymentAgent,
  type DeploymentWorkflow,
  type MaintenanceState,
  databaseUrl,
  keyIdentifier,
  randomSecret,
  sealForAgents,
  startWorkflow,
} from '@/lib/api/adminManagement';
import { isActive } from './workflowSteps';

type Run = (work: () => Promise<string | void>) => Promise<void>;

const BUTTON =
  'rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-surface-hover disabled:opacity-50';
const INPUT = 'rounded-md border border-border bg-surface px-2 py-1 text-sm text-foreground';
const SECTION = 'mt-6 rounded-lg border border-border p-4';
const URL_NAME = 'AGENT_DATABASE_URL';

const PHASES: Record<string, string> = {
  preflight: 'Target checked',
  fence: 'Old database fenced',
  copy: 'Data copied',
  verify: 'Copy verified',
  switch: 'Switched to the new database',
  complete: 'Finished',
};

/** The move as this control plane last heard it from the agent, with the real outage named. */
export function MaintenancePanel({ maintenance }: { maintenance?: MaintenanceState }) {
  if (!maintenance?.active) return null;
  const heard = Object.values(maintenance.progress ?? {}).flatMap((agent) => agent.operations);
  const phase = heard
    .map((operation) => operation.phase)
    .filter((item): item is string => Boolean(item))
    .at(-1);
  return (
    <div role="status" className="mt-4 rounded-md border border-border bg-surface p-3 text-sm">
      <strong>Changes are refused while the database moves.</strong>{' '}
      {maintenance.since && <>Since {new Date(maintenance.since).toLocaleString()}. </>}
      {phase ? (
        <>Last step reported: {PHASES[phase] ?? phase}. </>
      ) : (
        <>No step reported to this process yet. </>
      )}
      Reading keeps working. When the move switches over, the control plane restarts: this page may
      stop answering for a minute or two, then shows the result from the new database.
    </div>
  );
}

interface FormState {
  host: string;
  port: string;
  database: string;
  user: string;
  password: string;
}

const EMPTY: FormState = { host: '', port: '5432', database: '', user: '', password: '' };

function Field({
  label,
  value,
  type = 'text',
  onChange,
}: {
  label: string;
  value: string;
  type?: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="flex flex-col text-xs">
      {label}
      <input
        className={INPUT}
        type={type}
        value={value}
        aria-label={label}
        autoComplete="off"
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

/** Same-database connection changes, the bundled password, and moving to another database. */
export function DatabaseOperations({
  controlPlane,
  workflows,
  run,
}: {
  controlPlane: DeploymentAgent[];
  workflows: DeploymentWorkflow[];
  run: Run;
}) {
  const current = controlPlane[0]?.observed.database ?? null;
  const [mode, setMode] = useState<'connection' | 'move'>('connection');
  const [form, setForm] = useState<FormState>(EMPTY);
  const hasTools = controlPlane.some((agent) => agent.observed.policy?.has_database_tools);
  const busy = workflows.some(
    (workflow) => isActive(workflow) && workflow.kind.startsWith('database_'),
  );
  const set = (name: keyof FormState) => (value: string) => setForm({ ...form, [name]: value });
  const useCurrent = mode === 'connection' && current;
  const address = (password: string): DatabaseAddress => ({
    host: form.host || (useCurrent ? current.host : ''),
    port: Number(form.port) || (useCurrent ? current.port : 5432),
    database: form.database || (useCurrent ? current.database : ''),
    user: form.user || (useCurrent ? current.user : ''),
    password,
    query: useCurrent ? current.query : undefined,
  });
  const complete = form.password && address(form.password).host && address(form.password).database;
  const start = async (kind: string, url: string, rotate: boolean) => {
    await startWorkflow({
      kind,
      params: { dsn_id: await keyIdentifier(url), rotate_password: rotate },
      sealed_by_agent: await sealForAgents(controlPlane, URL_NAME, url),
    });
    setForm(EMPTY);
  };
  return (
    <section className={SECTION} aria-labelledby="database">
      <h2 id="database" className="text-base font-semibold">
        Database
      </h2>
      {current ? (
        <p className="mt-1 text-sm text-muted">
          Now connected to <code>{current.database}</code> on{' '}
          <code>
            {current.host}:{current.port}
          </code>{' '}
          as <code>{current.user}</code>.
        </p>
      ) : (
        <p className="mt-1 text-sm text-muted">
          No control-plane agent reports a connection yet, so these operations are not available.
        </p>
      )}
      <div className="mt-2 flex flex-wrap gap-2">
        <button
          type="button"
          className={BUTTON}
          disabled={!current || busy}
          onClick={() => {
            if (
              window.confirm(
                'Generate a new database password and set it? The old password stops working at once; if the switch then fails, it can only be fixed forward with the new one, which stays on the machines.',
              )
            ) {
              void run(() =>
                start(
                  'database_connection',
                  databaseUrl({ ...current!, password: randomSecret() }),
                  true,
                ),
              );
            }
          }}
        >
          Rotate the database password
        </button>
      </div>
      <fieldset className="mt-3">
        <legend className="text-sm font-medium">Change where the data is reached</legend>
        <label className="mr-4 text-sm">
          <input
            type="radio"
            name="database-mode"
            checked={mode === 'connection'}
            onChange={() => setMode('connection')}
          />{' '}
          Same database, new address or account
        </label>
        <label className="text-sm">
          <input
            type="radio"
            name="database-mode"
            checked={mode === 'move'}
            onChange={() => setMode('move')}
          />{' '}
          Move the data to an empty database
        </label>
      </fieldset>
      <p className="mt-1 text-xs text-muted">
        {mode === 'connection'
          ? 'A live probe proves the new connection reaches this same database before anything changes. Leave a field empty to keep its current value.'
          : 'Every process stops accepting changes, then the data is copied to the target and verified. A target with any table is refused. The old database stays read-only and is not switched back to.'}
      </p>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        <Field label="Database host" value={form.host} onChange={set('host')} />
        <Field label="Database port" value={form.port} onChange={set('port')} />
        <Field label="Database name" value={form.database} onChange={set('database')} />
        <Field label="Database user" value={form.user} onChange={set('user')} />
        <Field
          label="Database password"
          type="password"
          value={form.password}
          onChange={set('password')}
        />
        <button
          type="button"
          className={BUTTON}
          disabled={!complete || busy || !current || (mode === 'move' && !hasTools)}
          onClick={() => {
            const url = databaseUrl(address(form.password));
            const question =
              mode === 'move'
                ? 'Move every record to this empty database? Changes are refused until it finishes, and the service restarts on the new database.'
                : 'Change the connection to this same database?';
            if (!window.confirm(question)) return;
            void run(() =>
              start(mode === 'move' ? 'database_migration' : 'database_connection', url, false),
            );
          }}
        >
          {mode === 'move' ? 'Move to this database' : 'Change connection'}
        </button>
      </div>
      {mode === 'move' && !hasTools && (
        <p className="mt-1 text-xs text-muted">
          A move needs the PostgreSQL client programs named in the control-plane machine&apos;s
          local policy (<code>database_tools</code>).
        </p>
      )}
      <p className="mt-2 text-xs text-muted">
        If the control plane cannot be reached, the agent still finishes or resumes a move by
        itself. On the machine,{' '}
        <code>python -m deploy.agent --root &lt;root&gt; database status</code> shows where it
        stands, and <code>database unfence --operation-id &lt;id&gt;</code> reopens the old database
        before the switch.
      </p>
    </section>
  );
}
