'use client';

import { useId, useMemo, useState } from 'react';

import { Select, type SelectOption } from '@/components/ui/Select';
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

import {
  choiceLabel,
  itemDescription,
  itemLabel,
  keyHelp,
  keyLabel,
  serviceList,
} from './deploymentCopy';
import {
  BUTTON_CLASS,
  FieldRow,
  INPUT_CLASS,
  PRIMARY_CLASS,
  type Run,
  useConfirmation,
} from './deploymentUi';

/** Keys with their own guided operation; applying one directly would skip its evidence. */
const OWN_OPERATION = /^(security|database)\./;
/** A select's "leave it alone", distinct from any real choice, including an empty one. */
const KEEP = '\u0000keep';

type Field = [name: string, spec: ConfigKeyContract];

function FieldInput({
  id,
  name,
  spec,
  allowed,
  value,
  onChange,
}: {
  id: string;
  name: string;
  spec: ConfigKeyContract;
  allowed: string[] | undefined;
  value: string | undefined;
  onChange: (value: string | undefined) => void;
}) {
  const choices = allowed ?? (spec.choices.length ? spec.choices : null);
  if (choices) {
    const options: SelectOption[] = [
      { value: KEEP, label: 'Keep current value' },
      ...choices.map((choice) => ({ value: choice, label: choiceLabel(name, choice) })),
    ];
    return (
      <Select
        id={id}
        value={value ?? KEEP}
        options={options}
        onValueChange={(next) => onChange(next === KEEP ? undefined : next)}
        className={INPUT_CLASS}
        aria-describedby={`${id}-help`}
      />
    );
  }
  const secret = spec.kind === 'secret';
  return (
    <input
      id={id}
      className={INPUT_CLASS}
      type={secret ? 'password' : spec.kind === 'port' ? 'number' : 'text'}
      inputMode={spec.kind === 'port' ? 'numeric' : undefined}
      min={spec.kind === 'port' ? 1 : undefined}
      max={spec.kind === 'port' ? 65535 : undefined}
      value={value ?? ''}
      autoComplete={secret ? 'new-password' : 'off'}
      spellCheck={false}
      placeholder={secret ? 'Enter a new value to replace it' : 'Unchanged'}
      aria-describedby={`${id}-help`}
      onChange={(event) => onChange(event.target.value || undefined)}
    />
  );
}

function shownValue(name: string, spec: ConfigKeyContract, value: string): string {
  if (spec.kind === 'secret') return 'New value · hidden';
  if (spec.choices.length || spec.kind === 'choice') return choiceLabel(name, value);
  return value;
}

/** One machine's deployment configuration by what it does, each value in its own words. */
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
  const id = useId();
  const items = useMemo(() => {
    const grouped = new Map<string, Field[]>();
    for (const [name, spec] of Object.entries(contract?.config_keys[agent.role] ?? {})) {
      if (OWN_OPERATION.test(spec.item)) continue;
      grouped.set(spec.item, [...(grouped.get(spec.item) ?? []), [name, spec]]);
    }
    return grouped;
  }, [contract, agent.role]);
  const [chosen, setChosen] = useState('');
  const [values, setValues] = useState<Record<string, string>>({});
  const [confirm, dialog] = useConfirmation();
  const item = chosen || [...items.keys()][0] || '';
  const fields = items.get(item) ?? [];
  const filled = fields.filter(([name]) => values[name] !== undefined);
  if (items.size === 0) return null;
  const entry = catalog?.items.find((candidate) => candidate.id === item);
  const description = entry ? itemDescription(entry) : '';

  const apply = () =>
    run(async () => {
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
      return `Sent to ${agent.name}. It applies the change and keeps it only if the affected services come back healthy.`;
    });

  const review = () => {
    const services = [...new Set(filled.flatMap(([, spec]) => spec.services))];
    const restarts = services.length
      ? `The ${serviceList(services)} ${services.length > 1 ? 'restart' : 'restarts'}. `
      : '';
    confirm({
      title: `Apply to ${agent.name}?`,
      confirmLabel: 'Apply changes',
      action: () => void apply(),
      body: (
        <>
          <dl className="divide-y divide-border rounded-xl border border-border">
            {filled.map(([name, spec]) => (
              <div key={name} className="grid gap-1 px-4 py-3 sm:grid-cols-2 sm:gap-4">
                <dt className="text-foreground">{keyLabel(name, spec.kind)}</dt>
                <dd className="break-words font-medium text-foreground [overflow-wrap:anywhere]">
                  {shownValue(name, spec, values[name])}
                </dd>
              </div>
            ))}
          </dl>
          <p>
            {restarts}
            The machine saves this as a new configuration version and keeps it only if everything
            comes back healthy; otherwise it returns to the current one.
          </p>
          {filled.some(([, spec]) => spec.boundary === 'grow') && (
            <p>Protected ranges can only be added to, never narrowed.</p>
          )}
          {filled.some(([, spec]) => spec.kind === 'secret') && (
            <p>Secret values are encrypted for this machine before they leave your browser.</p>
          )}
        </>
      ),
    });
  };

  return (
    <form
      className="border-t border-border/70"
      onSubmit={(event) => {
        event.preventDefault();
        review();
      }}
    >
      <div className="px-4 pt-4 sm:px-5">
        <h4 className="text-sm font-semibold text-foreground">Configuration</h4>
        <p className="mt-1 text-xs leading-5 text-muted">
          Leave a value empty to keep it. Only what you change is sent.
        </p>
      </div>
      <FieldRow label="Area" help={description} labelId={`${id}-area`}>
        <Select
          value={item}
          aria-labelledby={`${id}-area`}
          className={INPUT_CLASS}
          options={[...items.keys()].map((key) => ({
            value: key,
            label: itemLabel(catalog?.items.find((candidate) => candidate.id === key) ?? key),
          }))}
          onValueChange={(next) => {
            setChosen(next);
            setValues({});
          }}
        />
      </FieldRow>
      {fields.map(([name, spec]) => {
        const fieldId = `${id}-${name}`;
        return (
          <FieldRow
            key={name}
            label={keyLabel(name, spec.kind)}
            help={<span id={`${fieldId}-help`}>{keyHelp(name)}</span>}
            htmlFor={fieldId}
          >
            <FieldInput
              id={fieldId}
              name={name}
              spec={spec}
              allowed={agent.observed.policy?.allowed?.[name]}
              value={values[name]}
              onChange={(value) => {
                const next = { ...values };
                if (value === undefined) delete next[name];
                else next[name] = value;
                setValues(next);
              }}
            />
          </FieldRow>
        );
      })}
      <div className="flex flex-wrap items-center justify-end gap-2 border-t border-border/60 px-4 py-3 sm:px-5">
        {filled.length > 0 && (
          <button type="button" className={BUTTON_CLASS} onClick={() => setValues({})}>
            Discard
          </button>
        )}
        <button type="submit" className={PRIMARY_CLASS} disabled={filled.length === 0}>
          Review changes
        </button>
      </div>
      {dialog}
    </form>
  );
}
