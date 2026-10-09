'use client';

import { useState } from 'react';

const AGENT_NAMES: Record<string, string> = {
  'claude-code': 'Claude Code',
  codex: 'Codex',
  pi: 'pi',
  opencode: 'OpenCode',
  kilo: 'Kilo',
  dsh: 'DeepSeek Harness',
};

export function agentName(runtime: string): string {
  return AGENT_NAMES[runtime] ?? runtime;
}

function VersionRow({
  runtime,
  versions,
  saved,
  busy,
  onChange,
}: {
  runtime: string;
  versions: string[];
  saved: string;
  busy: boolean;
  onChange: (runtime: string, version: string) => void;
}) {
  const [draft, setDraft] = useState(saved);
  const unavailable = saved !== '' && !versions.includes(saved);
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
        {unavailable && (
          <p className="mt-1 text-xs text-amber-700 dark:text-amber-300">
            The saved version is no longer available. Servers use their installed default.
          </p>
        )}
      </div>
      <div className="flex items-center justify-end gap-2">
        <select
          id={`agent-version-${runtime}`}
          disabled={busy}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          className="min-w-0 rounded-lg border border-gray-200 bg-white px-3 py-1.5 text-sm text-gray-800 dark:border-white/10 dark:bg-gray-900 dark:text-gray-200 disabled:opacity-40"
        >
          <option value="">Server default</option>
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
  selected,
  busy,
  onChange,
}: {
  menu: [string, string[]][];
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
        Default versions for new sandboxes. Existing chats and their forks keep their version. A
        server that does not have the selected version uses its installed default.
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
