'use client';

// Runtime settings — time limits, the repository import limit, chats per
// sandbox, terminal limits — set here instead of in each server's `.env`.
//
// Two places, one precedence: the card sets a value for every server, and a
// server's own panel can set one that wins over it. Clearing a value falls back
// a level, ending at the server's own `.env` or built-in default. Nothing here
// claims a value is in force until the server reports applying it.

import Link from 'next/link';
import { useState } from 'react';

import type { AgentRunnerHost, HostSettingDefinition, HostSettingValues } from '@/lib/api/admin';

import {
  factNote,
  formatSettingValue,
  parseSettingInput,
  rolloutText,
  settingFact,
  supportsSettings,
  unitLabel,
} from './settings';

type OnChange = (key: string, value: number | null) => void;

const INPUT_CLASS =
  'w-24 shrink-0 rounded-md border border-gray-200 bg-white px-1.5 py-0.5 text-xs tabular-nums text-gray-700 shadow-sm disabled:opacity-40 aria-[invalid=true]:border-red-400 dark:border-white/10 dark:bg-gray-900 dark:text-gray-200';
const BUTTON_CLASS =
  'rounded-md border border-gray-200 bg-white px-1.5 py-0.5 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40 dark:border-white/10 dark:bg-transparent dark:text-gray-200 dark:hover:bg-white/5';
const CLEAR_CLASS =
  'rounded-md px-1.5 py-0.5 text-xs text-gray-500 hover:bg-gray-50 hover:text-gray-700 disabled:opacity-40 dark:text-gray-400 dark:hover:bg-white/5 dark:hover:text-gray-200';

/**
 * One value's input, Save and clear.
 *
 * Uncontrolled and re-keyed on the saved value, like the execution-slots
 * field: a poll landing mid-edit does not overwrite what the admin is typing,
 * and a saved change does replace it.
 */
function SettingForm({
  definition,
  saved,
  placeholder,
  label,
  clearLabel,
  busy,
  disabled = false,
  onChange,
}: {
  definition: HostSettingDefinition;
  saved: number | null;
  placeholder: number | null;
  label: string;
  clearLabel: string;
  busy: boolean;
  disabled?: boolean;
  onChange: OnChange;
}) {
  const [error, setError] = useState<string | null>(null);
  const [dirty, setDirty] = useState(false);
  const unit = unitLabel(definition);
  const errorId = `setting-error-${label.replace(/[^a-zA-Z0-9_-]/g, '-')}`;
  return (
    <div>
      <form
        className="flex flex-wrap items-center justify-end gap-1.5 whitespace-nowrap"
        onSubmit={(event) => {
          event.preventDefault();
          const parsed = parseSettingInput(
            definition,
            String(new FormData(event.currentTarget).get('value') ?? ''),
          );
          if ('error' in parsed) {
            setError(parsed.error);
            return;
          }
          setError(null);
          if (parsed.value !== saved) onChange(definition.key, parsed.value);
        }}
      >
        <input
          key={`${definition.key}:${saved ?? 'unset'}`}
          name="value"
          type="number"
          inputMode="numeric"
          min={definition.minimum}
          max={definition.maximum}
          step={1}
          defaultValue={saved ?? ''}
          onChange={(event) => {
            setDirty(event.target.value !== String(saved ?? ''));
            setError(null);
          }}
          placeholder={placeholder === null ? undefined : String(placeholder)}
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
        <span className="w-7 text-xs text-gray-500 dark:text-gray-400">{unit}</span>
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
          className={`${CLEAR_CLASS} ${saved === null ? 'hidden sm:block sm:invisible' : ''}`}
        >
          {clearLabel}
        </button>
      </form>
      {error !== null && (
        <p
          id={errorId}
          role="alert"
          className="mt-1 text-right text-[11px] text-red-600 dark:text-red-400"
        >
          {error}
        </p>
      )}
    </div>
  );
}

/** The values set for every server, each with how far it has reached. */
export function GlobalSettingsCard({
  title,
  definitions,
  values,
  hosts,
  busy,
  onChange,
}: {
  title: string;
  definitions: HostSettingDefinition[];
  values: HostSettingValues;
  hosts: AgentRunnerHost[];
  busy: boolean;
  onChange: OnChange;
}) {
  return (
    <div className="mt-8 rounded-2xl border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-900">
      <h2 className="text-base font-semibold text-gray-900 dark:text-gray-50">{title}</h2>
      <div className="mt-2">
        {definitions.map((definition, index) => {
          const saved = values[definition.key] ?? null;
          return (
            <div
              key={definition.key}
              className={`flex flex-col gap-3 py-3 sm:flex-row sm:items-start ${
                index > 0 ? 'border-t border-gray-100 dark:border-white/5' : ''
              }`}
            >
              <div className="min-w-0 flex-1">
                <p className="text-sm font-medium text-gray-800 dark:text-gray-200">
                  {definition.label}
                  {saved !== null && (
                    <span className="ml-2 font-normal tabular-nums text-gray-500 dark:text-gray-400">
                      {formatSettingValue(definition, saved)}
                    </span>
                  )}
                </p>
                <p className="mt-0.5 text-xs text-gray-500 dark:text-gray-400">
                  {definition.description}
                </p>
                <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
                  {rolloutText(definition, values, hosts)}
                </p>
              </div>
              <div className="shrink-0">
                <SettingForm
                  key={`${definition.key}:${saved ?? 'unset'}`}
                  definition={definition}
                  saved={saved}
                  placeholder={definition.default}
                  label={`${definition.label} for all servers`}
                  clearLabel="Use server defaults"
                  busy={busy}
                  onChange={onChange}
                />
              </div>
            </div>
          );
        })}
      </div>
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
    <section aria-label={`Limits for ${name}`} className="mt-6">
      <h3 className="text-sm font-semibold text-gray-900 dark:text-gray-100">Limits</h3>
      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
        Values set here override the global defaults in{' '}
        <Link
          href="/admin/settings"
          className="underline hover:text-gray-700 dark:hover:text-gray-200"
        >
          Settings
        </Link>
        .
      </p>
      {!supported && (
        <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
          This server&apos;s runner predates settings from this page and keeps its .env values.
          Redeploy it to manage them here.
        </p>
      )}
      <table className="mt-2 w-full text-left text-sm">
        <thead className="text-xs text-gray-500 dark:text-gray-400">
          <tr>
            <th className="py-1.5 pr-3 font-medium">Setting</th>
            <th className="py-1.5 pr-3 font-medium">In use</th>
            <th className="py-1.5 text-right font-medium">This server only</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100 dark:divide-white/5">
          {definitions.map((definition) => {
            const fact = settingFact(definition, globalValues, host);
            const override = host.settings_desired?.[definition.key] ?? null;
            return (
              <tr key={definition.key}>
                <th
                  scope="row"
                  className="py-2 pr-3 align-top font-normal text-gray-800 dark:text-gray-200"
                >
                  {definition.label}
                </th>
                <td className="py-2 pr-3 align-top">
                  <span className="block whitespace-nowrap tabular-nums text-gray-800 dark:text-gray-200">
                    {fact.inUse === null ? '—' : formatSettingValue(definition, fact.inUse)}
                  </span>
                  {/* A runner that cannot apply settings says so once, above
                      the table, rather than on every row. */}
                  {supported && (
                    <span
                      className={`block text-[11px] ${
                        fact.state === 'applied'
                          ? 'text-gray-500 dark:text-gray-400'
                          : 'text-amber-700 dark:text-amber-300'
                      }`}
                    >
                      {factNote(fact)}
                    </span>
                  )}
                </td>
                <td className="py-2 align-top">
                  <SettingForm
                    key={`${definition.key}:${override ?? 'unset'}`}
                    definition={definition}
                    saved={override}
                    placeholder={fact.admin ?? fact.own ?? definition.default}
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
    </section>
  );
}
