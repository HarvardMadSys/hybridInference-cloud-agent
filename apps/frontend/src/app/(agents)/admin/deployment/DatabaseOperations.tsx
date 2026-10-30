'use client';

import { useId, useState } from 'react';

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

import { when } from './deploymentCopy';
import {
  BUTTON_CLASS,
  type Confirmation,
  Empty,
  FieldRow,
  GroupCard,
  GuideLink,
  INPUT_CLASS,
  PRIMARY_CLASS,
  type Run,
  Value,
  useConfirmation,
} from './deploymentUi';
import { isActive } from './workflowSteps';

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
  const since = when(maintenance.since);
  return (
    <div
      role="status"
      className="mt-6 rounded-xl border border-amber-500/30 bg-amber-500/5 px-4 py-3 text-sm leading-6 text-foreground sm:px-5"
    >
      <p className="font-semibold">Changes are paused while the database moves.</p>
      <p className="mt-1 text-muted">
        {since && <>Started {since}. </>}
        {phase ? (
          <>Last step reported: {PHASES[phase] ?? 'In progress'}. </>
        ) : (
          <>No step has been reported to this server yet. </>
        )}
        You can keep reading. When the move switches over, the service restarts: this page may stop
        answering for a minute or two, then shows the result from the new database.
      </p>
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

const FIELDS: { name: keyof FormState; label: string; help: string; type?: string }[] = [
  { name: 'host', label: 'Database host', help: 'The server name or address.' },
  { name: 'port', label: 'Database port', help: 'Usually 5432.', type: 'number' },
  { name: 'database', label: 'Database name', help: 'The database on that server.' },
  { name: 'user', label: 'Database user', help: 'The account the application signs in as.' },
  {
    name: 'password',
    label: 'Database password',
    help: 'Encrypted for the control-plane machines before it leaves your browser.',
    type: 'password',
  },
];

type Mode = 'connection' | 'move';

const MODES: { id: Mode; title: string; description: string }[] = [
  {
    id: 'connection',
    title: 'Same database, new address or account',
    description:
      'Nothing is copied. A live check proves the new connection reaches this same database before anything changes.',
  },
  {
    id: 'move',
    title: 'Move the data to an empty database',
    description:
      'Changes are paused, the data is copied and verified, then the service switches over. The old database is kept read-only.',
  },
];

function ModeChoice({ mode, onChange }: { mode: Mode; onChange: (mode: Mode) => void }) {
  return (
    <div role="radiogroup" aria-label="What to change" className="grid gap-2 sm:grid-cols-2">
      {MODES.map((option) => (
        <label
          key={option.id}
          className={`flex cursor-pointer gap-3 rounded-xl border p-3.5 transition-colors focus-within:ring-2 focus-within:ring-crimson dark:focus-within:ring-crimson-light ${
            mode === option.id
              ? 'border-accent/40 bg-accent/5'
              : 'border-border hover:bg-surface-hover'
          }`}
        >
          <input
            type="radio"
            name="database-mode"
            className="mt-1 accent-[rgb(var(--ui-accent))]"
            checked={mode === option.id}
            onChange={() => onChange(option.id)}
          />
          <span className="min-w-0">
            <span className="block text-sm font-medium text-foreground">{option.title}</span>
            <span className="mt-1 block text-xs leading-5 text-muted">{option.description}</span>
          </span>
        </label>
      ))}
    </div>
  );
}

/** Same-database connection changes, the database password, and moving to another database. */
export function DatabaseOperations({
  controlPlane,
  workflows,
  run,
}: {
  controlPlane: DeploymentAgent[];
  workflows: DeploymentWorkflow[];
  run: Run;
}) {
  const id = useId();
  const current = controlPlane[0]?.observed.database ?? null;
  const [mode, setMode] = useState<Mode>('connection');
  const [form, setForm] = useState<FormState>(EMPTY);
  const [confirm, dialog] = useConfirmation();
  const hasTools = controlPlane.some((agent) => agent.observed.policy?.has_database_tools);
  const busy = workflows.some(
    (workflow) => isActive(workflow) && workflow.kind.startsWith('database_'),
  );
  const useCurrent = mode === 'connection' && current;
  const address = (password: string): DatabaseAddress => ({
    host: form.host || (useCurrent ? current.host : ''),
    port: Number(form.port) || (useCurrent ? current.port : 5432),
    database: form.database || (useCurrent ? current.database : ''),
    user: form.user || (useCurrent ? current.user : ''),
    password,
    query: useCurrent ? current.query : undefined,
  });
  const target = address(form.password);
  const complete = Boolean(form.password && target.host && target.database);
  const start = async (kind: string, url: string, rotate: boolean) => {
    await startWorkflow({
      kind,
      params: { dsn_id: await keyIdentifier(url), rotate_password: rotate },
      sealed_by_agent: await sealForAgents(controlPlane, URL_NAME, url),
    });
    setForm(EMPTY);
  };
  const placeholder = (name: keyof FormState) =>
    useCurrent && name !== 'password' ? `Current: ${current[name]}` : '';

  const rotatePassword = () =>
    confirm({
      title: 'Rotate the database password?',
      danger: true,
      confirmLabel: 'Rotate password',
      body: (
        <>
          <p>A new password is generated in your browser and set on the database.</p>
          <p>
            The old password stops working at once. If switching the machines over then fails, it is
            fixed forward with the new password, which stays on the machines.
          </p>
        </>
      ),
      action: () =>
        void run(() =>
          start(
            'database_connection',
            databaseUrl({ ...current!, password: randomSecret() }),
            true,
          ),
        ),
    });

  const where = (
    <>
      <strong className="text-foreground">{target.database}</strong> on{' '}
      <strong className="text-foreground">
        {target.host}:{target.port}
      </strong>
    </>
  );
  const move: Confirmation = {
    title: 'Move every record to this database?',
    danger: true,
    confirmLabel: 'Start move',
    body: (
      <>
        <p>Moving to {where}. A target that already holds any table is refused.</p>
        <p>
          Changes are paused until the move finishes, and the service restarts on the new database.
          The old database stays read-only and is never switched back to automatically.
        </p>
      </>
    ),
    action: () => void run(() => start('database_migration', databaseUrl(target), false)),
  };
  const connect: Confirmation = {
    title: 'Change the connection to this same database?',
    confirmLabel: 'Switch connection',
    body: (
      <p>
        Connecting to {where} as <strong className="text-foreground">{target.user}</strong>. Nothing
        changes unless a live check shows it reaches the same data.
      </p>
    ),
    action: () => void run(() => start('database_connection', databaseUrl(target), false)),
  };
  const submit = () => confirm(mode === 'move' ? move : connect);

  return (
    <>
      <GroupCard
        title="Current connection"
        icon="link"
        actions={
          <button
            type="button"
            className={BUTTON_CLASS}
            disabled={!current || busy}
            onClick={rotatePassword}
          >
            Rotate password
          </button>
        }
      >
        {current ? (
          <>
            <FieldRow label="Server" help="As reported by the control-plane machine.">
              <Value mono>
                {current.host}:{current.port}
              </Value>
            </FieldRow>
            <FieldRow label="Database">
              <Value mono>{current.database}</Value>
            </FieldRow>
            <FieldRow label="Account">
              <Value mono>{current.user}</Value>
            </FieldRow>
          </>
        ) : (
          <Empty>
            No control-plane machine reports its database connection yet, so these changes are not
            available.
          </Empty>
        )}
      </GroupCard>
      <GroupCard title="Change the database" icon="settings">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <FieldRow
            label="What to change"
            help={
              mode === 'connection'
                ? 'Leave a field empty to keep its current value.'
                : 'Enter the empty database the data should move to.'
            }
          >
            <ModeChoice mode={mode} onChange={setMode} />
          </FieldRow>
          {FIELDS.map((field) => (
            <FieldRow
              key={field.name}
              label={field.label}
              help={field.help}
              htmlFor={`${id}-${field.name}`}
            >
              <input
                id={`${id}-${field.name}`}
                className={INPUT_CLASS}
                type={field.type ?? 'text'}
                value={form[field.name]}
                placeholder={placeholder(field.name)}
                autoComplete={field.type === 'password' ? 'new-password' : 'off'}
                spellCheck={false}
                onChange={(event) => setForm({ ...form, [field.name]: event.target.value })}
              />
            </FieldRow>
          ))}
          {mode === 'move' && !hasTools && (
            <p className="border-b border-border/60 px-4 py-3 text-xs leading-5 text-muted sm:px-5">
              Moving needs the PostgreSQL client tools on the control-plane machine, and its local
              policy does not list them yet. Setting them up is described in{' '}
              <GuideLink section="Moving the database" />.
            </p>
          )}
          <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-5">
            <p className="max-w-xl text-xs leading-5 text-muted">
              If this page stops answering during a move, the control-plane machine finishes or
              resumes it on its own. Recovery steps are in{' '}
              <GuideLink section="Moving the database" />.
            </p>
            <button
              type="submit"
              className={PRIMARY_CLASS}
              disabled={!complete || busy || !current || (mode === 'move' && !hasTools)}
            >
              {mode === 'move' ? 'Move to this database' : 'Change connection'}
            </button>
          </div>
        </form>
      </GroupCard>
      {dialog}
    </>
  );
}
