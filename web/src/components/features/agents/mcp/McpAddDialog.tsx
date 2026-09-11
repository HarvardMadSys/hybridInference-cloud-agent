'use client';

import { useEffect, useRef, useState } from 'react';
import { McpImportStep } from './McpImportStep';
import {
  createMcpConnection,
  type CreateMcpConnectionRequest,
  type McpAuthKind,
  type McpLaunchRequest,
  type McpLocation,
  type McpTemplateApi,
  type McpTransport,
} from '@/lib/api/mcp';

/** The three ways in, in the order that costs the user least effort first. */
const MODES = [
  { key: 'template', label: 'From a template' },
  { key: 'import', label: 'Import' },
  { key: 'manual', label: 'Manual' },
] as const;

type AddMode = (typeof MODES)[number]['key'];

const FIELD =
  'mt-1 w-full rounded-lg border border-gray-200 dark:border-white/10 bg-transparent px-2.5 py-2 text-sm text-gray-900 dark:text-gray-100';
const LABEL = 'block text-xs font-medium text-gray-700 dark:text-gray-300';

/** A blank manual draft. */
const EMPTY_FORM = {
  label: '',
  transport: 'streamable-http' as McpTransport,
  location: 'remote' as McpLocation,
  url: '',
  command: '',
  args: '',
  authKind: 'none' as McpAuthKind,
  secret: '',
  headerName: 'Authorization',
  headerScheme: 'Bearer',
};

/** Where each transport may run, which is a rule the backend enforces too. */
function transportsFor(location: McpLocation): McpTransport[] {
  return location === 'remote' ? ['streamable-http', 'http-sse'] : ['stdio'];
}

/** The argument list, as the user typed it. */
function argsOf(args: string): string[] {
  return args.split(/\s+/).filter(Boolean);
}

/** The argument vector, command first. */
function argvOf(command: string, args: string): string[] {
  return [command.trim(), ...argsOf(args)].filter(Boolean);
}

/**
 * Add one MCP connection, three ways.
 *
 * The paths differ in how much they ask of the user, not in what they may
 * create: a template only prefills fields, and a paste is always shown as a
 * draft — with its command vector and its masked secrets — before anything is
 * stored. That confirmation is the point of the import path rather than a
 * courtesy: a colleague's snippet that starts a local process is exactly the
 * case where a silent save would be a surprise.
 */
export function McpAddDialog({
  templates,
  onClose,
  onAdded,
}: {
  templates: McpTemplateApi[];
  onClose: () => void;
  onAdded: (labels: string) => void;
}) {
  const panel = useRef<HTMLDivElement>(null);
  const [mode, setMode] = useState<AddMode>(templates.length ? 'template' : 'manual');
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [envRows, setEnvRows] = useState<Array<{ name: string; value: string }>>([]);
  const [templateName, setTemplateName] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    panel.current?.querySelector<HTMLElement>('button, input, textarea, select')?.focus();
  }, []);

  function update<K extends keyof typeof EMPTY_FORM>(key: K, value: (typeof EMPTY_FORM)[K]) {
    setForm((current) => ({ ...current, [key]: value }));
  }

  function chooseLocation(location: McpLocation) {
    setForm((current) => {
      const allowed = transportsFor(location);
      const transport = allowed.includes(current.transport) ? current.transport : allowed[0];
      return { ...current, location, transport };
    });
  }

  function applyTemplate(template: McpTemplateApi) {
    setTemplateName(template.name);
    setForm({
      ...EMPTY_FORM,
      label: template.label,
      transport: template.transport,
      location: 'remote',
      url: template.url,
      authKind: template.auth_kind,
    });
    setError(null);
  }

  /** The request the current form would post, or the reason it cannot. */
  function manualRequest(): { body: CreateMcpConnectionRequest } | { problem: string } {
    const label = form.label.trim();
    if (!label) return { problem: 'Give the connection a label.' };
    if (form.location === 'remote') {
      if (!form.url.trim()) return { problem: 'A remote connection needs a URL.' };
      return {
        body: {
          label,
          transport: form.transport,
          location: 'remote',
          url: form.url.trim(),
          auth_kind: form.authKind,
          template: templateName ?? undefined,
          secret: form.authKind === 'static' ? form.secret : undefined,
          header_name: form.authKind === 'static' ? form.headerName : undefined,
          header_scheme: form.authKind === 'static' ? form.headerScheme : undefined,
        },
      };
    }
    if (!form.command.trim()) return { problem: 'A local process needs a command.' };
    const declared = envRows.filter((row) => row.name.trim());
    const missing = declared.filter((row) => !row.value.trim()).map((row) => row.name.trim());
    if (missing.length) {
      // Saving an unresolved `secret://NAME` would produce a connection that
      // looks configured and authenticates with nothing, and no later screen
      // sets a stdio environment secret.
      return { problem: `Give a value for ${missing.join(', ')}, or remove the row.` };
    }
    // The reference and the value are stored apart: the launch spec carries
    // `secret://NAME` so a plan cannot be read without its secrets, and the
    // values travel in `env_secrets` where they are encrypted.
    const env: Record<string, string> = {};
    const envSecrets: Record<string, string> = {};
    for (const row of declared) {
      const name = row.name.trim();
      env[name] = `secret://${name}`;
      if (row.value.trim()) envSecrets[name] = row.value;
    }
    const launch: McpLaunchRequest = {
      command: form.command.trim(),
      args: argsOf(form.args),
      env,
      cwd: null,
    };
    return {
      body: {
        label,
        transport: 'stdio',
        location: form.location,
        launch,
        auth_kind: form.authKind === 'static' ? 'static' : 'none',
        env_secrets: envSecrets,
        secret: form.authKind === 'static' ? form.secret : undefined,
        header_name: form.authKind === 'static' ? form.headerName : undefined,
        header_scheme: form.authKind === 'static' ? form.headerScheme : undefined,
      },
    };
  }

  async function save() {
    const prepared = manualRequest();
    if ('problem' in prepared) {
      setError(prepared.problem);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const created = await createMcpConnection(prepared.body);
      onAdded(created.label);
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not add the connection.');
    } finally {
      setSaving(false);
    }
  }

  const isRemote = form.location === 'remote';
  const argv = isRemote ? [] : argvOf(form.command, form.args).filter(Boolean);
  const declaredEnv = envRows.filter((row) => row.name.trim());
  const activeTemplate = templates.find((item) => item.name === templateName);

  return (
    <div
      className="fixed inset-0 z-[60] flex items-center justify-center p-4"
      role="presentation"
      onKeyDown={(event) => {
        if (event.key === 'Escape') {
          event.stopPropagation();
          onClose();
        }
      }}
    >
      <button
        type="button"
        aria-label="Close add connection dialog"
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-gray-900/25 backdrop-blur-[2px] dark:bg-black/50"
      />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label="Add an MCP connection"
        className="relative flex max-h-full w-full max-w-2xl flex-col overflow-hidden rounded-xl border border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 shadow-card"
      >
        <div className="flex shrink-0 items-center justify-between border-b border-gray-200 dark:border-white/10 px-5 py-3.5">
          <h2 className="text-[15px] font-semibold text-gray-900 dark:text-gray-100">
            Add an MCP connection
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md px-2 py-1 text-gray-500 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-white/5"
          >
            Close
          </button>
        </div>

        <nav
          aria-label="Add methods"
          className="flex shrink-0 gap-1 border-b border-gray-200 dark:border-white/10 px-5 text-sm"
        >
          {MODES.map((item) => (
            <button
              key={item.key}
              type="button"
              aria-pressed={mode === item.key}
              onClick={() => {
                setMode(item.key);
                setError(null);
              }}
              className={`-mb-px border-b-2 px-3 py-2.5 ${
                mode === item.key
                  ? 'border-gray-900 dark:border-gray-100 font-medium text-gray-900 dark:text-gray-50'
                  : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-100'
              }`}
            >
              {item.label}
            </button>
          ))}
        </nav>

        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {mode === 'import' ? (
            <McpImportStep onAdded={onAdded} />
          ) : (
            <>
              {mode === 'template' ? (
                <div>
                  <p className="text-sm text-gray-600 dark:text-gray-400">
                    Pick a preset, then check the details before adding. A template only fills in
                    the fields; it never decides what you may connect.
                  </p>
                  <div className="mt-3 flex flex-wrap gap-2">
                    {templates.map((template) => (
                      <button
                        key={template.name}
                        type="button"
                        onClick={() => applyTemplate(template)}
                        aria-pressed={templateName === template.name}
                        title={template.description}
                        className={`rounded-full border px-3 py-1.5 text-sm ${
                          templateName === template.name
                            ? 'border-gray-900 dark:border-gray-100 bg-gray-900 dark:bg-gray-100 text-white dark:text-gray-900'
                            : 'border-gray-200 dark:border-white/10 text-gray-700 dark:text-gray-300 hover:bg-gray-50 dark:hover:bg-white/5'
                        }`}
                      >
                        {template.label}
                      </button>
                    ))}
                    {templates.length === 0 ? (
                      <p className="text-sm text-gray-500 dark:text-gray-400">
                        This deployment offers no presets. Use the manual form.
                      </p>
                    ) : null}
                  </div>
                  {activeTemplate ? (
                    <div className="mt-4 rounded-xl border border-gray-200 dark:border-white/10 p-3">
                      <p className="text-sm text-gray-700 dark:text-gray-300">
                        {activeTemplate.description}
                      </p>
                      <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-gray-500 dark:text-gray-400">
                        {activeTemplate.notes.map((note) => (
                          <li key={note}>{note}</li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                </div>
              ) : null}

              {mode === 'manual' || templateName ? (
                <div className="mt-4 grid gap-3 sm:grid-cols-2">
                  <div className="sm:col-span-2">
                    <label className={LABEL} htmlFor="mcp-add-label">
                      Label
                    </label>
                    <input
                      id="mcp-add-label"
                      value={form.label}
                      onChange={(event) => update('label', event.target.value)}
                      placeholder="Team wiki"
                      className={FIELD}
                    />
                  </div>
                  <div>
                    <label className={LABEL} htmlFor="mcp-add-location">
                      Run location
                    </label>
                    <select
                      id="mcp-add-location"
                      value={form.location}
                      onChange={(event) => chooseLocation(event.target.value as McpLocation)}
                      className={FIELD}
                    >
                      <option value="remote">Remote server</option>
                      <option value="managed-worker">Managed worker</option>
                      <option value="user-connector">Your connector</option>
                    </select>
                  </div>
                  <div>
                    <label className={LABEL} htmlFor="mcp-add-transport">
                      Transport
                    </label>
                    <select
                      id="mcp-add-transport"
                      value={form.transport}
                      onChange={(event) => update('transport', event.target.value as McpTransport)}
                      className={FIELD}
                    >
                      {transportsFor(form.location).map((transport) => (
                        <option key={transport} value={transport}>
                          {transport}
                        </option>
                      ))}
                    </select>
                  </div>
                  {isRemote ? (
                    <div className="sm:col-span-2">
                      <label className={LABEL} htmlFor="mcp-add-url">
                        URL
                      </label>
                      <input
                        id="mcp-add-url"
                        value={form.url}
                        onChange={(event) => update('url', event.target.value)}
                        placeholder="https://mcp.example.com/mcp"
                        className={FIELD}
                      />
                    </div>
                  ) : (
                    <>
                      <div className="sm:col-span-2">
                        <label className={LABEL} htmlFor="mcp-add-command">
                          Command
                        </label>
                        <input
                          id="mcp-add-command"
                          value={form.command}
                          onChange={(event) => update('command', event.target.value)}
                          placeholder="npx"
                          className={FIELD}
                        />
                      </div>
                      <div className="sm:col-span-2">
                        <label className={LABEL} htmlFor="mcp-add-args">
                          Arguments, space separated
                        </label>
                        <input
                          id="mcp-add-args"
                          value={form.args}
                          onChange={(event) => update('args', event.target.value)}
                          placeholder="-y @modelcontextprotocol/server-filesystem /workspace"
                          className={FIELD}
                        />
                      </div>
                    </>
                  )}
                  <div>
                    <label className={LABEL} htmlFor="mcp-add-auth">
                      Authentication
                    </label>
                    <select
                      id="mcp-add-auth"
                      value={form.authKind}
                      onChange={(event) => update('authKind', event.target.value as McpAuthKind)}
                      className={FIELD}
                    >
                      <option value="none">None</option>
                      <option value="static">Static credential</option>
                      {isRemote ? <option value="oauth">OAuth</option> : null}
                    </select>
                  </div>
                  {form.authKind === 'static' ? (
                    <>
                      <div>
                        <label className={LABEL} htmlFor="mcp-add-header">
                          Header name
                        </label>
                        <input
                          id="mcp-add-header"
                          value={form.headerName}
                          onChange={(event) => update('headerName', event.target.value)}
                          className={FIELD}
                        />
                      </div>
                      <div>
                        <label className={LABEL} htmlFor="mcp-add-scheme">
                          Header scheme
                        </label>
                        <input
                          id="mcp-add-scheme"
                          value={form.headerScheme}
                          onChange={(event) => update('headerScheme', event.target.value)}
                          className={FIELD}
                        />
                      </div>
                      <div className="sm:col-span-2">
                        <label className={LABEL} htmlFor="mcp-add-secret">
                          Credential
                        </label>
                        <input
                          id="mcp-add-secret"
                          type="password"
                          value={form.secret}
                          onChange={(event) => update('secret', event.target.value)}
                          className={FIELD}
                        />
                        <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                          Stored encrypted. It is never shown again.
                        </p>
                      </div>
                    </>
                  ) : null}
                  {!isRemote ? (
                    <div className="sm:col-span-2">
                      <span className={LABEL}>Environment secrets</span>
                      {envRows.map((row, index) => (
                        <div key={index} className="mt-1 flex gap-2">
                          <input
                            aria-label={`Environment variable ${index + 1} name`}
                            value={row.name}
                            onChange={(event) =>
                              setEnvRows((rows) =>
                                rows.map((item, at) =>
                                  at === index ? { ...item, name: event.target.value } : item,
                                ),
                              )
                            }
                            placeholder="GITHUB_TOKEN"
                            className={FIELD}
                          />
                          <input
                            aria-label={`Environment variable ${index + 1} value`}
                            type="password"
                            value={row.value}
                            onChange={(event) =>
                              setEnvRows((rows) =>
                                rows.map((item, at) =>
                                  at === index ? { ...item, value: event.target.value } : item,
                                ),
                              )
                            }
                            className={FIELD}
                          />
                        </div>
                      ))}
                      <button
                        type="button"
                        onClick={() => setEnvRows((rows) => [...rows, { name: '', value: '' }])}
                        className="mt-2 text-sm text-gray-700 dark:text-gray-300 hover:underline"
                      >
                        Add environment secret
                      </button>
                    </div>
                  ) : null}
                </div>
              ) : null}

              <div className="mt-4 rounded-xl border border-gray-200 dark:border-white/10 bg-gray-50 dark:bg-white/5 p-3">
                <h3 className="text-xs font-semibold uppercase tracking-[0.08em] text-gray-500 dark:text-gray-400">
                  What will be saved
                </h3>
                <p className="mt-2 break-all font-mono text-xs text-gray-800 dark:text-gray-200">
                  {isRemote
                    ? form.url.trim() || 'No URL yet'
                    : argv.length
                      ? argv.join(' ')
                      : 'No command yet'}
                </p>
                {form.authKind === 'static' ? (
                  <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    {form.headerName}
                    {form.headerScheme ? `: ${form.headerScheme} ` : ': '}
                    {form.secret
                      ? '•'.repeat(Math.min(form.secret.length, 12))
                      : 'no credential yet'}
                  </p>
                ) : null}
                {declaredEnv.map((row) => (
                  <p key={row.name} className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    {row.name} = {row.value ? '•'.repeat(Math.min(row.value.length, 12)) : 'unset'}
                  </p>
                ))}
                {form.authKind === 'oauth' ? (
                  <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    You will authorize this connection in the browser after it is added.
                  </p>
                ) : null}
              </div>
            </>
          )}

          {error ? (
            <p role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">
              {error}
            </p>
          ) : null}
        </div>

        {mode !== 'import' ? (
          <div className="flex shrink-0 items-center justify-end gap-2 border-t border-gray-200 dark:border-white/10 px-5 py-3.5">
            <button
              type="button"
              onClick={onClose}
              className="rounded-lg border border-gray-200 dark:border-white/10 px-3 py-1.5 text-sm font-medium text-gray-700 dark:text-gray-300"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={saving || (mode === 'template' && !templateName)}
              onClick={() => void save()}
              className="rounded-lg bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-sm font-medium text-white dark:text-gray-900 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {saving ? 'Adding…' : 'Add connection'}
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}
