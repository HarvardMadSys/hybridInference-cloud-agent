'use client';

import { useState } from 'react';

import type { NamedHost } from '../hosts/HostSettings';

const AGENT_NAMES: Record<string, string> = {
  'claude-code': 'Claude Code',
  codex: 'Codex',
  pi: 'pi',
  opencode: 'OpenCode',
  kilo: 'Kilo Code',
};

export function agentName(runtime: string): string {
  return AGENT_NAMES[runtime] ?? runtime;
}

/** Name up to two servers, or count more, so a warning stays one line each. */
const NAMED_SERVERS = 2;

/**
 * "Server default", with the version it stands for.
 *
 * Each server runs its own image's default, so the label shows it when every
 * server agrees and says so when they do not.
 */
function defaultLabel(servers: NamedHost[], runtime: string): string {
  const versions = new Set(
    servers.flatMap(({ host }) => {
      const version = host.runtime_versions?.[runtime];
      return version ? [version] : [];
    }),
  );
  if (versions.size === 1) return `Server default (${[...versions][0]})`;
  if (versions.size > 1) return 'Server default (varies by server)';
  return 'Server default';
}

/**
 * Servers whose image lacks a version, with what their new sandboxes run instead.
 *
 * A selection a server cannot serve is not an error at claim time: that server
 * falls back to its own default (`resolve_runtime_version`). The page names
 * those servers so the fallback is not a surprise.
 */
function lacking(servers: NamedHost[], runtime: string, version: string) {
  return servers.flatMap(({ name, host }) =>
    (host.runtime_versions_available?.[runtime] ?? []).includes(version)
      ? []
      : [{ name, fallback: host.runtime_versions?.[runtime] ?? null }],
  );
}

function LackingNote({
  missing,
  pending,
}: {
  missing: { name: string; fallback: string | null }[];
  pending: boolean;
}) {
  const verb = pending ? 'would use' : 'use';
  if (missing.length === 0) return null;
  if (missing.length > NAMED_SERVERS) {
    return (
      <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
        Not on {missing.length} servers: new sandboxes there {verb} their default.
      </p>
    );
  }
  return (
    <>
      {missing.map(({ name, fallback }) => (
        <p key={name} className="mt-1 text-xs text-amber-700 dark:text-amber-300">
          Not on {name}: new sandboxes there {verb} {fallback ?? 'its default'}.
        </p>
      ))}
    </>
  );
}

function VersionRow({
  runtime,
  versions,
  servers,
  saved,
  busy,
  onChange,
}: {
  runtime: string;
  versions: string[];
  servers: NamedHost[];
  saved: string;
  busy: boolean;
  onChange: (runtime: string, version: string) => void;
}) {
  const [draft, setDraft] = useState(saved);
  const unavailable = saved !== '' && !versions.includes(saved);
  const pending = draft !== saved;
  const shown = pending ? draft : saved;
  // A version no server has gets its own line below; a partial gap names servers.
  const missing = shown === '' || (!pending && unavailable) ? [] : lacking(servers, runtime, shown);
  return (
    <form
      className="flex flex-col gap-3 py-3 sm:flex-row sm:items-center sm:justify-between"
      onSubmit={(event) => {
        event.preventDefault();
        if (draft !== saved) onChange(runtime, draft);
      }}
    >
      <div>
        <label
          htmlFor={`agent-version-${runtime}`}
          className="text-sm font-medium text-gray-800 dark:text-gray-200"
        >
          {agentName(runtime)}
        </label>
        {unavailable && !pending && (
          <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
            The saved version is no longer available. Servers use their installed default.
          </p>
        )}
        <LackingNote missing={missing} pending={pending} />
      </div>
      <div className="flex items-center justify-end gap-2">
        <select
          id={`agent-version-${runtime}`}
          disabled={busy}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          className="min-w-0 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-800 dark:border-white/10 dark:bg-gray-900 dark:text-gray-200 disabled:opacity-40"
        >
          <option value="">{defaultLabel(servers, runtime)}</option>
          {unavailable && (
            <option value={saved} disabled>
              {saved} (unavailable)
            </option>
          )}
          {versions.map((version) => (
            <option key={version} value={version}>
              {version}
            </option>
          ))}
        </select>
        <button
          type="submit"
          disabled={busy || draft === saved}
          className="rounded-md border border-gray-200 px-2 py-1 text-xs font-medium text-gray-700 hover:bg-gray-50 disabled:opacity-40 dark:border-white/10 dark:text-gray-200 dark:hover:bg-white/5"
        >
          Save
        </button>
      </div>
    </form>
  );
}

export default function AgentVersions({
  menu,
  servers,
  selected,
  busy,
  onChange,
}: {
  menu: [string, string[]][];
  servers: NamedHost[];
  selected: Record<string, string>;
  busy: boolean;
  onChange: (runtime: string, version: string) => void;
}) {
  // Keep a saved selection visible if the last server offering it goes away.
  const entries = new Map(menu);
  for (const runtime of Object.keys(selected)) {
    if (!entries.has(runtime)) entries.set(runtime, []);
  }
  return (
    <section className="mt-8 rounded-2xl border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-900">
      <h2 className="text-base font-semibold text-gray-900 dark:text-gray-50">Agent versions</h2>
      <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
        Default versions for new sandboxes. Existing sessions and their forks keep their version.
      </p>
      {entries.size === 0 ? (
        <p className="mt-4 text-sm text-gray-500 dark:text-gray-400">
          No server has reported available versions yet.
        </p>
      ) : (
        <div className="mt-2 divide-y divide-gray-100 dark:divide-white/5">
          {[...entries]
            .sort(([a], [b]) => a.localeCompare(b))
            .map(([runtime, versions]) => (
              <VersionRow
                key={`${runtime}:${selected[runtime] ?? ''}`}
                runtime={runtime}
                versions={versions}
                servers={servers}
                saved={selected[runtime] ?? ''}
                busy={busy}
                onChange={onChange}
              />
            ))}
        </div>
      )}
    </section>
  );
}
