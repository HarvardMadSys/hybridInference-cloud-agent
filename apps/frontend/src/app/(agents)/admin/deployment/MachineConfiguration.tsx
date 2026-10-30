'use client';

import { useMemo, useState } from 'react';
import {
  type ConfigKeyContract,
  type DeploymentAgent,
  type DeploymentOverview,
  type ManagementCatalog,
  type SealedBox,
  createIntent,
  seal,
  sealedPurpose,
} from '@/lib/api/adminManagement';

type Run = (work: () => Promise<string | void>) => Promise<void>;

const BUTTON =
  'rounded-md border border-border px-3 py-1.5 text-sm font-medium text-foreground hover:bg-surface-hover disabled:opacity-50';
const INPUT = 'rounded-md border border-border bg-surface px-2 py-1 text-sm text-foreground';

/** Keys with their own guided operation; applying one directly would skip its evidence. */
const OWN_OPERATION = /^(security|database)\./;

const FIELD_NAMES: Record<string, string> = {
  bind: 'Listen address',
  port: 'Port',
  url: 'Address',
  path: 'Path',
  name: 'Name',
};

function fieldLabel(name: string, spec: ConfigKeyContract): string {
  return `${FIELD_NAMES[spec.kind] ?? 'Value'} (${name})`;
}

function itemTitle(item: string, catalog: ManagementCatalog | null): string {
  const label = catalog?.items.find((entry) => entry.id === item)?.label;
  if (label) return label;
  const words = item.split('.').at(-1)?.replaceAll('_', ' ') ?? item;
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function FieldInput({
  name,
  spec,
  allowed,
  value,
  onChange,
}: {
  name: string;
  spec: ConfigKeyContract;
  allowed: string[] | undefined;
  value: string;
  onChange: (value: string) => void;
}) {
  const label = fieldLabel(name, spec);
  const options = allowed ?? (spec.choices.length ? spec.choices : null);
  return (
    <label className="flex flex-col text-xs">
      {label}
      {options ? (
        <select
          className={INPUT}
          value={value}
          aria-label={label}
          onChange={(event) => onChange(event.target.value)}
        >
          <option value="">Keep as it is</option>
          {options.map((option) => (
            <option key={option} value={option}>
              {option || 'None'}
            </option>
          ))}
        </select>
      ) : (
        <input
          className={INPUT}
          type={spec.kind === 'secret' ? 'password' : spec.kind === 'port' ? 'number' : 'text'}
          min={spec.kind === 'port' ? 1 : undefined}
          max={spec.kind === 'port' ? 65535 : undefined}
          value={value}
          aria-label={label}
          autoComplete="off"
          placeholder="Keep as it is"
          onChange={(event) => onChange(event.target.value)}
        />
      )}
    </label>
  );
}

/** One machine's deployment configuration by what it does, each field of its own type. */
export function MachineConfiguration({
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
  const items = useMemo(() => {
    const grouped = new Map<string, [string, ConfigKeyContract][]>();
    for (const [name, spec] of Object.entries(contract?.config_keys[agent.role] ?? {})) {
      if (OWN_OPERATION.test(spec.item)) continue;
      grouped.set(spec.item, [...(grouped.get(spec.item) ?? []), [name, spec]]);
    }
    return grouped;
  }, [contract, agent.role]);
  const [chosen, setChosen] = useState('');
  const [values, setValues] = useState<Record<string, string>>({});
  const item = chosen || [...items.keys()][0] || '';
  const fields = items.get(item) ?? [];
  const filled = fields.filter(([name]) => values[name]);
  const restarts = [...new Set(filled.flatMap(([, spec]) => spec.services))];
  if (items.size === 0) return null;
  return (
    <form
      className="mt-3"
      onSubmit={(event) => {
        event.preventDefault();
        void run(async () => {
          const changes: Record<string, string> = {};
          const sealed: Record<string, SealedBox> = {};
          for (const [name, spec] of filled) {
            if (spec.kind === 'secret') {
              sealed[name] = await seal(
                agent.public_key,
                values[name],
                sealedPurpose(agent.agent_id, name),
              );
            } else {
              changes[name] = values[name];
            }
          }
          await createIntent(agent.agent_id, {
            operation: 'apply_config',
            params: { changes },
            ...(Object.keys(sealed).length ? { sealed } : {}),
          });
          setValues({});
        });
      }}
    >
      <label className="text-xs">
        Configure
        <select
          className={`${INPUT} ml-1`}
          value={item}
          onChange={(event) => {
            setChosen(event.target.value);
            setValues({});
          }}
        >
          {[...items.keys()].map((id) => (
            <option key={id} value={id}>
              {itemTitle(id, catalog)}
            </option>
          ))}
        </select>
      </label>
      <div className="mt-2 flex flex-wrap items-end gap-2">
        {fields.map(([name, spec]) => (
          <FieldInput
            key={name}
            name={name}
            spec={spec}
            allowed={agent.observed.policy?.allowed?.[name]}
            value={values[name] ?? ''}
            onChange={(value) => setValues({ ...values, [name]: value })}
          />
        ))}
        <button type="submit" className={BUTTON} disabled={filled.length === 0}>
          Apply
        </button>
      </div>
      {filled.length > 0 && (
        <p className="mt-1 text-xs text-muted">
          The change becomes a new configuration version on this machine and is kept only if{' '}
          {restarts.join(', ')} come back healthy.
          {filled.some(([, spec]) => spec.boundary === 'grow') ? ' Ranges can only be added.' : ''}
          {filled.some(([, spec]) => spec.kind === 'secret')
            ? ' Secrets are sealed to this machine before they leave the browser.'
            : ''}
        </p>
      )}
    </form>
  );
}
