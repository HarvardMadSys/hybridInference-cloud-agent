'use client';

import { useEffect, useState, type ReactNode } from 'react';
import { McpImportStep } from './McpImportStep';
import { McpServerIcon } from './McpServerIcon';
import {
  listMcpCatalog,
  type McpCatalogApi,
  type McpCatalogEntryApi,
  type McpConnectionApi,
} from '@/lib/api/mcp';

const BUTTON =
  'rounded-lg border border-gray-200 px-3 py-1.5 text-xs text-gray-700 hover:bg-gray-50 disabled:opacity-50 dark:border-white/10 dark:text-gray-300 dark:hover:bg-white/5';

/** Search synced public metadata, then review a configuration before adding it. */
export function McpCatalogBrowser({
  fallback,
  disabled = false,
  onAdded,
  onBusyChange,
}: {
  fallback: ReactNode;
  disabled?: boolean;
  onAdded: (connections: McpConnectionApi[]) => void;
  onBusyChange: (busy: boolean) => void;
}) {
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [retry, setRetry] = useState(0);
  const [data, setData] = useState<McpCatalogApi | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(false);
  const [selected, setSelected] = useState<McpCatalogEntryApi | null>(null);
  const [option, setOption] = useState(0);
  const [busy, setBusy] = useState(false);
  const [useToken, setUseToken] = useState(false);
  const [view, setView] = useState<'recommended' | 'all'>('recommended');
  const recommended = !query.trim() && view === 'recommended';

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(false);
    const timer = setTimeout(
      () => {
        void listMcpCatalog(query.trim(), offset, recommended)
          .then((response) => {
            if (!cancelled) setData(response);
          })
          .catch(() => {
            if (!cancelled) setError(true);
          })
          .finally(() => {
            if (!cancelled) setLoading(false);
          });
      },
      query ? 250 : 0,
    );
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [query, offset, retry, recommended]);

  const blocked = busy || disabled;

  function changeBusy(value: boolean) {
    setBusy(value);
    onBusyChange(value);
  }

  if (selected) {
    const configuration = selected.configurations[option];
    const config = configuration ? JSON.parse(configuration.json) : null;
    const server = config
      ? (Object.values(config.mcpServers)[0] as { url?: string; headers?: Record<string, string> })
      : null;
    const canUseToken = Boolean(server?.url && !server.headers);
    if (canUseToken && useToken && server)
      server.headers = { Authorization: 'Bearer ${env:MCP_TOKEN}' };
    const configText =
      canUseToken && useToken ? JSON.stringify(config, null, 2) : configuration?.json;
    return (
      <div>
        <button
          type="button"
          disabled={blocked}
          onClick={() => setSelected(null)}
          className={BUTTON}
        >
          Back to servers
        </button>
        <h3 className="mt-4 flex items-center gap-3 text-base font-semibold text-gray-900 dark:text-gray-100">
          <McpServerIcon name={selected.name} label={selected.label} iconUrl={selected.icon_url} />
          <span>{selected.label}</span>
        </h3>
        <p className="mt-1 break-all text-xs text-gray-500 dark:text-gray-400">{selected.name}</p>
        <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">{selected.description}</p>
        <a
          href={`https://registry.modelcontextprotocol.io/?q=${encodeURIComponent(selected.name)}`}
          target="_blank"
          rel="noreferrer"
          className="mt-2 mr-4 inline-block text-xs text-blue-600 dark:text-blue-400"
        >
          Registry details ↗
        </a>
        {selected.website_url ? (
          <a
            href={selected.website_url}
            target="_blank"
            rel="noreferrer"
            className="mt-2 inline-block text-xs text-blue-600 dark:text-blue-400"
          >
            Server documentation ↗
          </a>
        ) : null}
        {selected.configurations.length > 1 ? (
          <label className="mt-4 block text-xs font-medium text-gray-700 dark:text-gray-300">
            Connection method
            <select
              aria-label="Connection method"
              value={option}
              disabled={blocked}
              onChange={(event) => {
                setOption(Number(event.target.value));
                setUseToken(false);
              }}
              className="mt-1 w-full rounded-lg border border-gray-200 bg-transparent px-3 py-2 text-sm dark:border-white/10"
            >
              {selected.configurations.map((item, index) => (
                <option key={index} value={index}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
        ) : null}
        {canUseToken ? (
          <label className="mt-4 block text-xs font-medium text-gray-700 dark:text-gray-300">
            Authentication
            <select
              aria-label="Authentication"
              value={useToken ? 'token' : 'auto'}
              disabled={blocked}
              onChange={(event) => setUseToken(event.target.value === 'token')}
              className="mt-1 w-full rounded-lg border border-gray-200 bg-transparent px-3 py-2 text-sm dark:border-white/10"
            >
              <option value="auto">Detect from server</option>
              <option value="token">Bearer access token</option>
            </select>
          </label>
        ) : null}
        {configuration ? (
          <div className="mt-4">
            <p className="mb-3 text-xs text-gray-500 dark:text-gray-400">
              Configuration from the official MCP Registry · version {selected.version}. Review it
              before adding.
            </p>
            <McpImportStep
              key={`${selected.name}:${option}:${useToken}`}
              initialText={configText}
              onAdded={onAdded}
              onBusyChange={changeBusy}
            />
          </div>
        ) : (
          <p className="mt-4 text-sm text-gray-600 dark:text-gray-400">
            This server needs additional setup. Follow its documentation, then paste its
            configuration in the JSON tab.
          </p>
        )}
      </div>
    );
  }

  const available = Boolean(data?.synced_at);
  return (
    <div>
      <label className="block text-xs font-medium text-gray-700 dark:text-gray-300">
        Search MCP servers
        <input
          type="search"
          disabled={blocked}
          value={query}
          maxLength={200}
          onChange={(event) => {
            setQuery(event.target.value);
            setOffset(0);
          }}
          placeholder="Search all servers by name, publisher or description"
          className="mt-2 w-full rounded-lg border border-gray-200 bg-transparent px-3 py-2 text-sm text-gray-900 dark:border-white/10 dark:text-gray-100"
        />
      </label>
      <div className="mb-4 mt-2 flex flex-wrap items-center justify-between gap-2 text-xs text-gray-500 dark:text-gray-400">
        <a
          href="https://registry.modelcontextprotocol.io/"
          target="_blank"
          rel="noreferrer"
          className="hover:underline"
        >
          Official MCP Registry ↗
        </a>
      </div>
      <nav aria-label="Directory view" className="mb-3 flex gap-2">
        {(
          [
            ['recommended', 'Recommended'],
            ['all', 'All servers'],
          ] as const
        ).map(([value, label]) => (
          <button
            key={value}
            type="button"
            disabled={blocked}
            aria-pressed={!query.trim() && view === value}
            className={`${BUTTON} ${!query.trim() && view === value ? 'bg-gray-100 font-medium dark:bg-white/10' : ''}`}
            onClick={() => {
              setView(value);
              setQuery('');
              setOffset(0);
            }}
          >
            {label}
          </button>
        ))}
      </nav>
      {data?.stale && available ? (
        <p role="status" className="mb-3 text-xs text-amber-700 dark:text-amber-300">
          Showing saved directory results while the next sync completes.
        </p>
      ) : null}
      {loading ? (
        <p role="status" className="py-4 text-sm text-gray-500 dark:text-gray-400">
          Loading servers…
        </p>
      ) : error || !available ? (
        <div>
          <div
            role="status"
            className="mb-4 flex items-center justify-between gap-3 text-xs text-gray-500 dark:text-gray-400"
          >
            <span>
              {error
                ? 'The directory is temporarily unavailable.'
                : 'The directory is waiting for its first sync.'}{' '}
              You can use these built-in servers or add JSON.
            </span>
            <button type="button" className={BUTTON} onClick={() => setRetry((value) => value + 1)}>
              Retry
            </button>
          </div>
          {fallback}
        </div>
      ) : (
        <>
          <p className="mb-3 text-xs text-gray-500 dark:text-gray-400">
            {recommended
              ? 'Selected for common coding workflows. Search or browse all servers for more.'
              : `${data?.total.toLocaleString()} ${query.trim() ? 'matching servers' : 'servers'}`}
          </p>
          <div className="grid gap-3 sm:grid-cols-2">
            {data?.entries.map((entry) => (
              <button
                type="button"
                key={entry.name}
                aria-label={entry.label}
                onClick={() => {
                  setSelected(entry);
                  setOption(0);
                  setUseToken(false);
                }}
                className="flex min-w-0 flex-col items-start rounded-xl border border-gray-200 p-4 text-left hover:bg-gray-50 dark:border-white/10 dark:hover:bg-white/5"
              >
                <span className="flex items-center gap-3 text-sm font-semibold text-gray-900 dark:text-gray-100">
                  <McpServerIcon name={entry.name} label={entry.label} iconUrl={entry.icon_url} />
                  <span>{entry.label}</span>
                </span>
                <span className="mt-1 block break-all text-[11px] text-gray-500 dark:text-gray-400">
                  {entry.name}
                </span>
                <span className="mt-2 line-clamp-2 text-xs leading-5 text-gray-500 dark:text-gray-400">
                  {entry.description}
                </span>
                {!entry.configurations.length ? (
                  <span className="mt-2 block text-[11px] text-gray-500 dark:text-gray-400">
                    Additional setup required
                  </span>
                ) : null}
              </button>
            ))}
          </div>
          {!data?.entries.length ? (
            <p className="py-4 text-sm text-gray-500 dark:text-gray-400">
              {recommended
                ? 'No recommended servers available. Browse all servers or add a server in the JSON tab.'
                : 'No matching servers. Try another name or add a server in the JSON tab.'}
            </p>
          ) : null}
          {offset > 0 || data?.next_offset != null ? (
            <div className="mt-4 flex justify-between gap-3">
              <button
                type="button"
                className={BUTTON}
                disabled={!offset || blocked}
                onClick={() => setOffset(Math.max(0, offset - 24))}
              >
                Previous
              </button>
              <button
                type="button"
                className={BUTTON}
                disabled={data?.next_offset == null || blocked}
                onClick={() => setOffset(data?.next_offset ?? 0)}
              >
                Next
              </button>
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
