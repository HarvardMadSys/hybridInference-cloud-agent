'use client';

import { useEffect, useRef, useState } from 'react';
import { McpImportStep } from './McpImportStep';
import { McpCatalogBrowser } from './McpCatalogBrowser';
import { McpServerIcon } from './McpServerIcon';
import {
  createMcpConnection,
  type McpAuthKind,
  type McpConnectionApi,
  type McpTemplateApi,
} from '@/lib/api/mcp';

export type McpAddMode = 'servers' | 'json';

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

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    panel.current?.querySelector<HTMLElement>('button, textarea')?.focus();
    return () => previous?.focus();
  }, []);

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

  const notes = selected?.auth_kind === authKind ? selected.notes : [];

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-4"
      onKeyDown={(event) => {
        if (event.key === 'Escape' && !saving) {
          event.stopPropagation();
          onClose();
        }
        if (event.key === 'Tab') {
          const controls = Array.from(
            panel.current?.querySelectorAll<HTMLElement>(
              'button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href]',
            ) ?? [],
          ).filter((control) => !control.closest('[hidden]'));
          if (!controls.length) return;
          const first = controls[0];
          const last = controls[controls.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        }
      }}
    >
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
          className="flex shrink-0 gap-5 border-b border-gray-200 dark:border-white/10 px-6"
        >
          {(
            [
              { key: 'servers', label: 'MCP servers' },
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
              className={`-mb-px border-b-2 py-3 text-sm ${mode === item.key ? 'border-gray-900 font-medium text-gray-900 dark:border-gray-100 dark:text-gray-100' : 'border-transparent text-gray-500 dark:text-gray-400'}`}
            >
              {item.label}
            </button>
          ))}
        </nav>
        <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
          <div hidden={mode !== 'json'}>
            <McpImportStep onAdded={onAdded} onBusyChange={setSaving} />
          </div>
          {mode === 'servers' ? (
            <McpCatalogBrowser
              disabled={saving}
              onAdded={onAdded}
              onBusyChange={setSaving}
              fallback={
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
                      <p className="break-all text-xs text-gray-500 dark:text-gray-400">
                        {selected.url}
                      </p>
                      {selected.name === 'github' ? (
                        <label className="mt-3 block text-xs font-medium text-gray-700 dark:text-gray-300">
                          Connect with
                          <select
                            disabled={saving}
                            value={authKind}
                            onChange={(event) => setAuthKind(event.target.value as McpAuthKind)}
                            className={FIELD}
                          >
                            <option value="oauth">GitHub OAuth app</option>
                            <option value="static">Personal access token</option>
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
                      Choose a service, or paste a custom server configuration in the JSON tab.
                    </p>
                  )}
                </div>
              }
            />
          ) : null}
        </div>
      </div>
    </div>
  );
}
