'use client';

import { useModalDialog } from '@/components/ui/useModalDialog';

import { useRef, useState } from 'react';
import { McpImportStep } from './McpImportStep';
import { McpCatalogBrowser } from './McpCatalogBrowser';
import { McpServerIcon } from './McpServerIcon';
import {
  createMcpConnection,
  type McpAuthKind,
  type McpConnectionApi,
  type McpTemplateApi,
} from '@/lib/api/mcp';

export type McpAddMode = 'servers' | 'directory' | 'json';

const FIELD =
  'mt-1 w-full rounded-lg border border-gray-200 dark:border-white/10 bg-transparent px-3 py-2 text-sm text-gray-900 dark:text-gray-100';

/** Known services and standard MCP configuration share the same connection flow. */
export function McpAddDialog({
  templates,
  initialMode = 'servers',
  onClose,
  onAdded,
}: {
  templates: McpTemplateApi[];
  initialMode?: McpAddMode;
  onClose: () => void;
  onAdded: (connections: McpConnectionApi[]) => void;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const submission = useRef(false);
  const [mode, setMode] = useState<McpAddMode>(initialMode);
  const [selected, setSelected] = useState<McpTemplateApi | null>(null);
  const [authKind, setAuthKind] = useState<McpAuthKind>('oauth');
  const [secret, setSecret] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useModalDialog(panel, { busy: saving, onClose });

  async function connect() {
    if (!selected || submission.current) return;
    submission.current = true;
    setSaving(true);
    setError(null);
    try {
      const connection = await createMcpConnection({
        label: selected.label,
        transport: selected.transport,
        location: 'remote',
        url: selected.url,
        auth_kind: authKind,
        template: selected.name,
        ...(authKind === 'static'
          ? { secret, header_name: 'Authorization', header_scheme: 'Bearer' }
          : {}),
      });
      onAdded([connection]);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not add this server.');
    } finally {
      submission.current = false;
      setSaving(false);
    }
  }

  let notes = selected?.notes ?? [];
  if (selected?.name === 'github' && selected.auth_kind !== authKind) {
    if (authKind === 'oauth') {
      notes = [
        'Register a GitHub OAuth app, then enter its client ID and client secret in the authorization step. Use the callback URL shown there.',
      ];
    } else {
      notes = [
        'Use a GitHub personal access token with access to the repositories your agents need.',
      ];
    }
  }

  const presetBrowser = (
    <div>
      <div className="grid gap-3 sm:grid-cols-2">
        {templates.map((template) => (
          <button
            key={template.name}
            type="button"
            disabled={saving}
            aria-label={template.label}
            aria-pressed={selected?.name === template.name}
            onClick={() => {
              setSelected(template);
              setAuthKind(template.auth_kind);
              setSecret('');
              setError(null);
            }}
            className={`rounded-xl border p-4 text-left transition-colors ${selected?.name === template.name ? 'border-gray-800 bg-gray-50 dark:border-gray-300 dark:bg-white/5' : 'border-gray-200 hover:bg-gray-50 dark:border-white/10 dark:hover:bg-white/5'}`}
          >
            <span className="flex items-center gap-3">
              <McpServerIcon name={template.name} label={template.label} />
              <span className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                {template.label}
              </span>
            </span>
            <span className="mt-3 block text-xs leading-5 text-gray-500 dark:text-gray-400">
              {template.description}
            </span>
          </button>
        ))}
      </div>
      {selected ? (
        <div className="mt-5 border-t border-gray-100 pt-4 dark:border-white/10">
          <p className="break-all text-xs text-gray-500 dark:text-gray-400">{selected.url}</p>
          {selected.documentation_url ? (
            <a
              href={selected.documentation_url}
              target="_blank"
              rel="noreferrer"
              className="mt-2 inline-block text-xs text-blue-600 dark:text-blue-400"
            >
              {selected.label} setup guide ↗
            </a>
          ) : null}
          {selected.name === 'github' ? (
            <label className="mt-3 block text-xs font-medium text-gray-700 dark:text-gray-300">
              Connect with
              <select
                disabled={saving}
                value={authKind}
                onChange={(event) => setAuthKind(event.target.value as McpAuthKind)}
                className={FIELD}
              >
                <option value="static">Personal access token</option>
                <option value="oauth">GitHub OAuth app</option>
              </select>
            </label>
          ) : null}
          {authKind === 'static' ? (
            <label className="mt-3 block text-xs font-medium text-gray-700 dark:text-gray-300">
              Access token
              <input
                type="password"
                autoComplete="off"
                value={secret}
                onChange={(event) => setSecret(event.target.value)}
                disabled={saving}
                className={FIELD}
              />
            </label>
          ) : (
            <p className="mt-3 text-sm text-gray-600 dark:text-gray-400">
              Sign in to {selected.label} to choose what your agents can access.
            </p>
          )}
          {notes.map((note) => (
            <p key={note} className="mt-2 text-xs text-gray-500 dark:text-gray-400">
              {note}
            </p>
          ))}
          {error ? (
            <p role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">
              {error}
            </p>
          ) : null}
          <button
            type="button"
            disabled={saving || (authKind === 'static' && !secret.trim())}
            onClick={() => void connect()}
            className="mt-4 rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900"
          >
            {saving ? 'Connecting…' : `Connect ${selected.label}`}
          </button>
        </div>
      ) : (
        <p className="mt-4 text-xs text-gray-500 dark:text-gray-400">
          Choose a service, browse the MCP directory, or paste a custom configuration in the JSON
          tab.
        </p>
      )}
    </div>
  );

  return (
    <div lang="en" className="fixed inset-0 z-[60] flex items-center justify-center p-4">
      <button
        type="button"
        aria-label="Close add server dialog"
        disabled={saving}
        onClick={onClose}
        tabIndex={-1}
        className="absolute inset-0 cursor-default bg-gray-900/25 backdrop-blur-[2px] dark:bg-black/50"
      />
      <div
        ref={panel}
        role="dialog"
        lang="en"
        aria-modal="true"
        aria-labelledby="mcp-add-title"
        className="relative flex max-h-full w-full max-w-2xl flex-col overflow-hidden rounded-2xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 shadow-card"
      >
        <div className="flex shrink-0 items-start justify-between gap-4 px-6 pt-5 pb-4">
          <div>
            <h2
              id="mcp-add-title"
              className="text-lg font-semibold text-gray-900 dark:text-gray-100"
            >
              Add MCP server
            </h2>
            <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
              Connect your tools and data to your agents.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="rounded-md px-2 py-1 text-sm text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-white/5"
          >
            Close
          </button>
        </div>
        <nav
          aria-label="Add methods"
          className="flex shrink-0 gap-5 overflow-x-auto border-b border-gray-200 dark:border-white/10 px-6"
        >
          {(
            [
              { key: 'servers', label: 'MCP servers' },
              { key: 'directory', label: 'MCP directory' },
              { key: 'json', label: 'JSON configuration' },
            ] as const
          ).map((item) => (
            <button
              key={item.key}
              type="button"
              disabled={saving}
              aria-pressed={mode === item.key}
              onClick={() => {
                setMode(item.key);
                setError(null);
              }}
              className={`-mb-px shrink-0 whitespace-nowrap border-b-2 py-3 text-sm ${mode === item.key ? 'border-gray-900 font-medium text-gray-900 dark:border-gray-100 dark:text-gray-100' : 'border-transparent text-gray-500 dark:text-gray-400'}`}
            >
              {item.label}
            </button>
          ))}
        </nav>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          <div hidden={mode !== 'json'}>
            <McpImportStep onAdded={onAdded} onBusyChange={setSaving} />
          </div>
          {mode === 'servers' ? presetBrowser : null}
          {mode === 'directory' ? (
            <McpCatalogBrowser
              disabled={saving}
              onAdded={onAdded}
              onBusyChange={setSaving}
              fallback={presetBrowser}
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}
