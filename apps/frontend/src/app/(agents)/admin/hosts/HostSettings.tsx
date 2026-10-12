'use client';

// Sandbox settings — container resources, turn and repository limits,
// sessions per sandbox, idle timeouts and terminal limits — set from the admin
// pages instead of in each server's `.env`.
//
// Two places, one precedence: the Settings page sets a value for every server,
// and a server's own panel on the Servers page can set one that wins over it.
// Clearing a value falls back a level, ending at the deployment's built-in
// default. Nothing here claims a value is in force until the server
// reports applying it.

import Link from 'next/link';
import { useState } from 'react';

import type { AgentRunnerHost, HostSettingDefinition, HostSettingValues } from '@/lib/api/admin';

import { SettingsSection } from '../SettingsSection';

import {
  amountIn,
  factNote,
  formatSettingValue,
  type InputUnit,
  inputUnits,
  parseSettingInput,
  rolloutText,
  settingFact,
  shorteningEffect,
  supportsSettings,
  unitFor,
} from './settings';

type OnChange = (key: string, value: number | null) => void;

/** A server as the admin pages name it, with the host that speaks for it. */
export interface NamedHost {
  name: string;
  host: AgentRunnerHost;
}

const INPUT_CLASS =
  'ui-input min-h-11 w-24 shrink-0 px-2 tabular-nums disabled:cursor-not-allowed disabled:opacity-50 aria-[invalid=true]:border-status-error';
const UNIT_CLASS =
  'ui-input min-h-11 w-20 shrink-0 px-2 disabled:cursor-not-allowed disabled:opacity-50';
const BUTTON_CLASS = 'ui-button ui-button-primary min-h-11 min-w-16';
const CLEAR_CLASS = 'ui-button ui-button-ghost min-h-11 text-xs';

interface SettingGroup {
  title: string;
  definitions: HostSettingDefinition[];
}

/** Keep the same organization for defaults and individual server overrides. */
function organizeSettings(definitions: HostSettingDefinition[]) {
  const resources: SettingGroup = { title: 'Container resources', definitions: [] };
  const sessions: SettingGroup = { title: 'Session limits', definitions: [] };
  const other: SettingGroup = { title: 'Other settings', definitions: [] };
  const terminals: SettingGroup = { title: 'Terminal limits', definitions: [] };
  const workspace: SettingGroup = { title: 'Workspace limits', definitions: [] };
  const storage: SettingGroup = { title: 'Storage admission', definitions: [] };

  for (const definition of definitions) {
    const key = definition.key;
    if (['sandbox_cpu_millicores', 'sandbox_memory_mib', 'sandbox_disk_mib'].includes(key)) {
      resources.definitions.push(definition);
    } else if (
      [
        'agent_timeout_s',
        'source_import_max_mib',
        'chat_idle_timeout_s',
        'shared_resident_capacity',
      ].includes(key)
    ) {
      sessions.definitions.push(definition);
    } else if (key.startsWith('terminal_')) {
      terminals.definitions.push(definition);
    } else if (key.startsWith('workspace_')) {
      workspace.definitions.push(definition);
    } else if (key === 'storage_min_available_mib') {
      storage.definitions.push(definition);
    } else {
      // New definitions stay visible until their group has been decided.
      other.definitions.push(definition);
    }
  }

  return [
    {
      title: 'Important' as const,
      description: 'Container resources and limits for running sessions.',
      groups: [resources, sessions, other].filter((group) => group.definitions.length > 0),
    },
    {
      title: 'Advanced' as const,
      description: 'Terminal, workspace and storage admission limits.',
      groups: [terminals, workspace, storage].filter((group) => group.definitions.length > 0),
    },
  ].filter((section) => section.groups.length > 0);
}

/**
 * One value's amount, unit, Save and clear.
 *
 * Re-keyed on the saved value by its parent, like the execution-slots field:
 * a poll landing mid-edit does not reset what the admin is typing, and a saved
 * change does replace it. The amount is typed in a unit — 8 h, not 28800 —
 * and sent in the setting's own seconds, MiB or millicores.
 */
function SettingForm({
  definition,
  saved,
  placeholder,
  label,
  clearLabel,
  busy,
  disabled = false,
  targetId,
  onChange,
}: {
  definition: HostSettingDefinition;
  saved: number | null;
  placeholder: number | null;
  label: string;
  clearLabel: string;
  busy: boolean;
  disabled?: boolean;
  targetId?: string;
  onChange: OnChange;
}) {
  // What this level uses now: its own value, else the one it inherits.
  const current = saved ?? placeholder;
  const units = inputUnits(definition);
  const startUnit = unitFor(definition, current ?? definition.default);
  const startAmount = saved === null ? '' : amountIn(saved, startUnit);
  const [unit, setUnit] = useState<InputUnit | null>(startUnit);
  const [amount, setAmount] = useState(startAmount);
  const [error, setError] = useState<string | null>(null);
  const dirty = amount !== startAmount || (amount !== '' && unit !== startUnit);
  const typed = amount.trim() === '' ? null : parseSettingInput(definition, amount, unit);
  const effect =
    typed !== null && 'value' in typed && current !== null && typed.value < current
      ? shorteningEffect(definition, typed.value)
      : null;
  const errorId = `setting-error-${label.replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  return (
    <div>
      <form
        noValidate
        className="flex flex-wrap items-center gap-2 sm:justify-end"
        onSubmit={(event) => {
          event.preventDefault();
          const parsed = parseSettingInput(definition, amount, unit);
          if ('error' in parsed) {
            setError(parsed.error);
            return;
          }
          setError(null);
          if (parsed.value !== saved) onChange(definition.key, parsed.value);
        }}
      >
        <input
          id={targetId}
          name="value"
          type="number"
          inputMode={units.length > 0 ? 'decimal' : 'numeric'}
          step="any"
          value={amount}
          onChange={(event) => {
            setAmount(event.target.value);
            setError(null);
          }}
          placeholder={current === null ? undefined : amountIn(current, unit)}
          aria-label={label}
          aria-invalid={error !== null}
          aria-describedby={error !== null ? errorId : undefined}
          disabled={busy || disabled}
          className={INPUT_CLASS}
        />
        {/* The unit and the clear button keep their space on every row, so the
            inputs line up down the column whether or not a row has a unit or
            a value to clear. A row with nothing to clear holds an inert,
            hidden button rather than none. */}
        {units.length > 0 ? (
          <select
            aria-label={`${label} unit`}
            value={unit ?? undefined}
            onChange={(event) => {
              setUnit(event.target.value as InputUnit);
              setError(null);
            }}
            disabled={busy || disabled}
            className={UNIT_CLASS}
          >
            {units.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        ) : (
          <span aria-hidden className="hidden w-20 shrink-0 sm:block" />
        )}
        <button type="submit" disabled={busy || disabled || !dirty} className={BUTTON_CLASS}>
          Save
        </button>
        <button
          type="button"
          disabled={busy || disabled || saved === null}
          aria-hidden={saved === null ? true : undefined}
          tabIndex={saved === null ? -1 : undefined}
          onClick={() => {
            setError(null);
            onChange(definition.key, null);
          }}
          className={`${CLEAR_CLASS} ${saved === null ? 'hidden sm:inline-flex sm:invisible' : ''}`}
        >
          {clearLabel}
        </button>
      </form>
      {error !== null ? (
        <p
          id={errorId}
          role="alert"
          className="mt-2 text-xs leading-5 text-status-error sm:text-right"
        >
          {error}
        </p>
      ) : (
        effect !== null && (
          <p className="mt-2 text-xs leading-5 text-status-warning sm:text-right">{effect}</p>
        )
      )}
    </div>
  );
}

/** The values set for every server, each with how far it has reached. */
export function GlobalSettingsCard({
  title,
  definitions,
  values,
  servers,
  busy,
  onChange,
}: {
  title: string;
  definitions: HostSettingDefinition[];
  values: HostSettingValues;
  servers: NamedHost[];
  busy: boolean;
  onChange: OnChange;
}) {
  const hosts = servers.map(({ host }) => host);
  const names = new Map(servers.map(({ name, host }) => [host.host, name] as const));
  const nameOf = (host: AgentRunnerHost) => names.get(host.host) ?? host.host;
  return (
    <div className="ui-card mt-6 p-4 sm:p-6">
      <h2 className="text-lg font-semibold tracking-tight text-foreground">{title}</h2>
      <p className="mt-2 text-sm leading-6 text-muted">
        Defaults for every server. Individual servers can override these values.
      </p>
      {organizeSettings(definitions).map((section) => (
        <SettingsSection
          key={section.title}
          title={section.title}
          description={section.description}
        >
          {section.groups.map((group) => (
            <div key={group.title} className="mt-5 first:mt-0">
              <h4 className="text-sm font-semibold text-foreground">{group.title}</h4>
              {group.definitions.map((definition, index) => {
                const saved = values[definition.key] ?? null;
                return (
                  <div
                    key={definition.key}
                    className={`grid gap-4 py-5 xl:grid-cols-[minmax(0,1fr)_auto] xl:items-start xl:gap-8 ${
                      index > 0 ? 'border-t border-border' : ''
                    }`}
                  >
                    <div className="min-w-0 flex-1">
                      <p className="text-sm font-medium text-foreground">
                        {definition.label}
                        {saved !== null && (
                          <span className="ml-2 inline-flex rounded-md bg-surface-muted px-2 py-0.5 text-xs font-normal tabular-nums text-muted">
                            {formatSettingValue(definition, saved)}
                          </span>
                        )}
                      </p>
                      <p className="mt-1.5 text-sm leading-6 text-muted">
                        {definition.description}
                      </p>
                      <p className="mt-2 text-xs leading-5 text-muted">
                        {rolloutText(definition, values, hosts, nameOf)}
                      </p>
                    </div>
                    <div className="min-w-0">
                      <SettingForm
                        key={`${definition.key}:${saved ?? 'unset'}`}
                        targetId={`host-setting-${definition.key}`}
                        definition={definition}
                        saved={saved}
                        placeholder={definition.default}
                        label={`${definition.label} for all servers`}
                        clearLabel="Use deployment default"
                        busy={busy}
                        onChange={onChange}
                      />
                    </div>
                  </div>
                );
              })}
            </div>
          ))}
        </SettingsSection>
      ))}
    </div>
  );
}

/** One server's values: what it uses, where from, and its own overrides. */
export function ServerSettingsPanel({
  name,
  host,
  definitions,
  globalValues,
  busy,
  onChange,
}: {
  name: string;
  host: AgentRunnerHost;
  definitions: HostSettingDefinition[];
  globalValues: HostSettingValues;
  busy: boolean;
  onChange: OnChange;
}) {
  const supported = supportsSettings(host);
  return (
    <section aria-label={`Sandbox settings for ${name}`} className="mt-6 min-w-0">
      <h3 className="text-base font-semibold text-foreground">Sandbox</h3>
      <p className="mt-2 text-sm leading-6 text-muted">
        Values set here override the global defaults in{' '}
        <Link
          href="/admin/settings"
          className="ui-focus-ring rounded text-accent underline underline-offset-4 hover:text-accent-hover"
        >
          Settings
        </Link>
        .
      </p>
      {!supported && (
        <p className="mt-3 rounded-xl border border-status-warning/20 bg-status-warning/5 p-3 text-sm leading-6 text-status-warning">
          This server&apos;s runner predates settings from this page and keeps its .env values.
          Redeploy it to manage them here.
        </p>
      )}
      <div className="mt-4 min-w-0" role="region" aria-label={`Sandbox settings table for ${name}`}>
        {organizeSettings(definitions).map((section) => (
          <SettingsSection
            key={section.title}
            title={section.title}
            description={section.description}
          >
            {section.groups.map((group) => (
              <div key={group.title} className="mt-5 first:mt-0">
                <h4 className="text-sm font-semibold text-foreground">{group.title}</h4>
                <div
                  className="ui-focus-ring mt-3 overflow-x-auto rounded-xl border border-border"
                  tabIndex={0}
                  role="region"
                  aria-label={`${group.title} table for ${name}`}
                >
                  <table className="w-full min-w-[36rem] text-left text-sm">
                    <caption className="sr-only">
                      {group.title} for {name}
                    </caption>
                    <thead className="border-b border-border bg-surface-muted text-xs text-muted">
                      <tr>
                        <th scope="col" className="px-4 py-3 font-medium">
                          Setting
                        </th>
                        <th scope="col" className="px-4 py-3 font-medium">
                          In use
                        </th>
                        <th scope="col" className="px-4 py-3 text-right font-medium">
                          This server only
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-border">
                      {group.definitions.map((definition) => {
                        const fact = settingFact(definition, globalValues, host);
                        const override = host.settings_desired?.[definition.key] ?? null;
                        return (
                          <tr key={definition.key}>
                            <th
                              scope="row"
                              className="px-4 py-4 align-top font-medium text-foreground"
                            >
                              {definition.label}
                            </th>
                            <td className="px-4 py-4 align-top">
                              <span className="block whitespace-nowrap tabular-nums text-foreground">
                                {fact.inUse === null
                                  ? '—'
                                  : formatSettingValue(definition, fact.inUse)}
                              </span>
                              {/* A runner that cannot apply settings says so once, above
                                  the tables, rather than on every row. */}
                              {supported && (
                                <span
                                  className={`mt-1 block text-xs leading-5 ${
                                    fact.state === 'applied' ? 'text-muted' : 'text-status-warning'
                                  }`}
                                >
                                  {factNote(fact)}
                                </span>
                              )}
                            </td>
                            <td className="px-4 py-4 align-top">
                              <SettingForm
                                key={`${definition.key}:${override ?? 'unset'}`}
                                definition={definition}
                                saved={override}
                                placeholder={fact.admin ?? definition.default}
                                label={`${definition.label} for ${name}`}
                                clearLabel="Use default"
                                busy={busy}
                                disabled={!supported}
                                onChange={onChange}
                              />
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            ))}
          </SettingsSection>
        ))}
      </div>
    </section>
  );
}
