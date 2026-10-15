'use client';

import { useState } from 'react';

import type { RuntimeUpgradeItem } from '@/lib/api/adminRuntimeUpgrades';

import DinoProgress from './DinoProgress';
import { SettingsSection } from '../SettingsSection';
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
      <p className="mt-2 text-xs leading-5 text-status-warning">
        Not on {missing.length} servers: new sandboxes there {verb} their default.
      </p>
    );
  }
  return (
    <>
      {missing.map(({ name, fallback }) => (
        <p key={name} className="mt-2 break-words text-xs leading-5 text-status-warning">
          Not on {name}: new sandboxes there {verb} {fallback ?? 'its default'}.
        </p>
      ))}
    </>
  );
}

/** What a rollback returns to, in words. */
function previousLabel(previous: string | null): string {
  return previous ?? 'the server default';
}

/**
 * Update one agent to npm's latest release, and follow the update through.
 *
 * Two states an admin waits on — downloading, then updated — and failed when
 * a server could not download or check the version, in which case new chats
 * keep the version they had.
 */
export function UpdateControls({
  item,
  busy,
  onUpdate,
  onRollback,
}: {
  item: RuntimeUpgradeItem;
  busy: boolean;
  onUpdate: (runtime: string, version: string) => void;
  onRollback: (runtime: string, version: string | null) => void;
}) {
  const name = agentName(item.runtime);
  const upgrade = item.upgrade;
  const rollback = upgrade?.activated_at ? (
    <button
      type="button"
      disabled={busy}
      onClick={() => onRollback(item.runtime, upgrade.previous_version)}
      className="ui-button ui-button-secondary min-h-11"
    >
      Roll back to {previousLabel(upgrade.previous_version)}
    </button>
  ) : null;

  if (upgrade?.state === 'downloading') {
    return (
      <DinoProgress
        progress={upgrade.progress}
        label={`Downloading ${name} ${upgrade.version} on every server`}
      />
    );
  }
  if (upgrade?.state === 'failed' && upgrade.kind === 'selection') {
    return (
      <div className="mt-3 space-y-2">
        <p role="alert" className="break-words text-xs leading-5 text-status-error">
          Some servers could not download {upgrade.version}; they start new chats on their own
          default.{upgrade.error ? ` ${upgrade.error}` : ''}
        </p>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => onUpdate(item.runtime, upgrade.version)}
            className="ui-button ui-button-primary min-h-11"
          >
            Try again
          </button>
        </div>
      </div>
    );
  }
  if (upgrade?.state === 'failed') {
    return (
      <div className="mt-3 space-y-2">
        <p role="alert" className="break-words text-xs leading-5 text-status-error">
          Updating to {upgrade.version} failed
          {upgrade.activated_at
            ? ''
            : `; new chats still use ${item.current_version ?? 'their version'}`}
          .{upgrade.error ? ` ${upgrade.error}` : ''}
        </p>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => onUpdate(item.runtime, upgrade.version)}
            className="ui-button ui-button-primary min-h-11"
          >
            Try again
          </button>
          {rollback}
        </div>
      </div>
    );
  }
  const updated =
    upgrade?.state === 'updated' && item.current_version === upgrade.version ? upgrade : null;
  return (
    <div className="mt-3 space-y-2">
      {updated && (
        <p className="text-xs leading-5 text-status-success">
          Updated to {updated.version}. New chats use it; existing chats keep their version.
        </p>
      )}
      {item.update_available && item.latest_version ? (
        <div className="flex flex-wrap items-center gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => onUpdate(item.runtime, item.latest_version as string)}
            className="ui-button ui-button-primary min-h-11"
          >
            Update to {item.latest_version}
          </button>
          {rollback}
        </div>
      ) : (
        <>
          {item.latest_version && !updated && (
            <p className="text-xs leading-5 text-muted">
              Up to date (latest {item.latest_version}).
            </p>
          )}
          {rollback && <div className="flex flex-wrap gap-2">{rollback}</div>}
        </>
      )}
      {item.latest_error && (
        <p className="break-words text-xs leading-5 text-muted">
          Could not check for a newer version: {item.latest_error}
        </p>
      )}
    </div>
  );
}

function VersionRow({
  runtime,
  versions,
  servers,
  saved,
  busy,
  onChange,
  update,
  updateBusy,
  onUpdate,
  onRollback,
}: {
  runtime: string;
  versions: string[];
  servers: NamedHost[];
  saved: string;
  busy: boolean;
  onChange: (runtime: string, version: string) => void;
  update?: RuntimeUpgradeItem;
  updateBusy: boolean;
  onUpdate: (runtime: string, version: string) => void;
  onRollback: (runtime: string, version: string | null) => void;
}) {
  const [draft, setDraft] = useState(saved);
  const unavailable = saved !== '' && !versions.includes(saved);
  const pending = draft !== saved;
  const shown = pending ? draft : saved;
  // A version no server has gets its own line below; a partial gap names servers.
  const missing = shown === '' || (!pending && unavailable) ? [] : lacking(servers, runtime, shown);
  return (
    <form
      className="grid gap-3 py-5 lg:grid-cols-[minmax(0,1fr)_minmax(240px,360px)] lg:items-center lg:gap-8"
      onSubmit={(event) => {
        event.preventDefault();
        if (draft !== saved) onChange(runtime, draft);
      }}
    >
      <div className="min-w-0">
        <label htmlFor={`agent-version-${runtime}`} className="text-sm font-medium text-foreground">
          {agentName(runtime)}
        </label>
        {unavailable && !pending && (
          <p className="mt-2 text-xs leading-5 text-status-warning">
            The saved version is no longer available. Servers use their installed default.
          </p>
        )}
        <LackingNote missing={missing} pending={pending} />
      </div>
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <select
            id={`agent-version-${runtime}`}
            disabled={busy}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            className="ui-input min-h-11 min-w-0 flex-1 disabled:cursor-not-allowed disabled:opacity-50"
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
            className="ui-button ui-button-primary min-h-11 min-w-16 shrink-0"
          >
            Save
          </button>
        </div>
        {pending && <p className="mt-2 text-xs text-muted">Unsaved change</p>}
      </div>
      {update && (
        <div className="min-w-0 lg:col-span-2">
          <UpdateControls
            item={update}
            busy={busy || updateBusy}
            onUpdate={onUpdate}
            onRollback={onRollback}
          />
        </div>
      )}
    </form>
  );
}

export default function AgentVersions({
  menu,
  servers,
  selected,
  busy,
  onChange,
  updates = null,
  updateBusy = false,
  updateError = null,
  onUpdate = () => {},
  onRollback = () => {},
}: {
  menu: [string, string[]][];
  servers: NamedHost[];
  selected: Record<string, string>;
  busy: boolean;
  onChange: (runtime: string, version: string) => void;
  updates?: RuntimeUpgradeItem[] | null;
  updateBusy?: boolean;
  updateError?: string | null;
  onUpdate?: (runtime: string, version: string) => void;
  onRollback?: (runtime: string, version: string | null) => void;
}) {
  const updateFor = new Map((updates ?? []).map((item) => [item.runtime, item]));
  // Keep a saved selection visible if the last server offering it goes away.
  const entries = new Map(menu);
  for (const runtime of Object.keys(selected)) {
    if (!entries.has(runtime)) entries.set(runtime, []);
  }
  const installedRuntimes = new Set([
    ...entries.keys(),
    ...servers.flatMap(({ host }) => Object.keys(host.runtime_versions ?? {})),
  ]);
  return (
    <section className="ui-card mt-6 p-4 sm:p-6">
      <h2 className="text-lg font-semibold tracking-tight text-foreground">Agent versions</h2>
      <p className="mt-2 text-sm leading-6 text-muted">
        Default versions for new sandboxes. Existing sessions and their forks keep their version.
      </p>
      <SettingsSection
        title="Important"
        description="Update an agent, or pick its version for new sandboxes."
      >
        {updateError && (
          <p role="alert" className="mt-4 break-words text-sm text-status-error">
            {updateError}
          </p>
        )}
        {entries.size === 0 ? (
          <p className="mt-5 rounded-xl bg-surface-muted p-4 text-sm text-muted">
            No server has reported available versions yet.
          </p>
        ) : (
          <div className="mt-4 divide-y divide-border">
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
                  update={updateFor.get(runtime)}
                  updateBusy={updateBusy}
                  onUpdate={onUpdate}
                  onRollback={onRollback}
                />
              ))}
          </div>
        )}
      </SettingsSection>
      <SettingsSection title="Advanced" description="Review the versions installed on each server.">
        {servers.length === 0 ? (
          <p className="py-4 text-sm text-muted">No active servers have reported versions yet.</p>
        ) : (
          <div className="overflow-x-auto py-3">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Agent versions installed on each server</caption>
              <thead>
                <tr className="border-b border-border text-muted">
                  <th scope="col" className="p-2 font-medium">
                    Server
                  </th>
                  <th scope="col" className="p-2 font-medium">
                    Agent
                  </th>
                  <th scope="col" className="p-2 font-medium">
                    Default
                  </th>
                  <th scope="col" className="p-2 font-medium">
                    Available versions
                  </th>
                </tr>
              </thead>
              <tbody>
                {servers.flatMap(({ name, host }) =>
                  [...installedRuntimes].sort().map((runtime) => (
                    <tr
                      key={`${host.host}:${runtime}`}
                      className="border-b border-border last:border-b-0"
                    >
                      <th scope="row" className="p-2 font-medium">
                        {name}
                      </th>
                      <td className="p-2">{agentName(runtime)}</td>
                      <td className="p-2">{host.runtime_versions?.[runtime] ?? 'Not reported'}</td>
                      <td className="p-2">
                        {host.runtime_versions_available?.[runtime]?.join(', ') || 'Not reported'}
                      </td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
          </div>
        )}
      </SettingsSection>
    </section>
  );
}
