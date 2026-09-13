'use client';

import { useState } from 'react';
import {
  previewMcpImport,
  saveMcpImport,
  type McpImportDraftApi,
  type McpImportPreviewApi,
  type McpLocation,
} from '@/lib/api/mcp';

const FIELD =
  'w-full rounded-lg border border-gray-200 dark:border-white/10 bg-transparent px-2.5 py-2 text-sm text-gray-900 dark:text-gray-100';

/** One draft's stored fields, rendered for confirmation. */
function DraftBody({ draft }: { draft: McpImportDraftApi }) {
  return (
    <dl className="mt-2 space-y-1 text-xs text-gray-500 dark:text-gray-400">
      <div className="flex gap-2">
        <dt className="shrink-0 font-medium text-gray-600 dark:text-gray-300">
          {draft.argv.length ? 'Command' : 'Address'}
        </dt>
        <dd className="break-all font-mono">
          {draft.argv.length ? draft.argv.join(' ') : (draft.url ?? 'none')}
        </dd>
      </div>
      {draft.env_names.length ? (
        <div className="flex gap-2">
          <dt className="shrink-0 font-medium text-gray-600 dark:text-gray-300">Environment</dt>
          <dd className="break-all">{draft.env_names.join(', ')}</dd>
        </div>
      ) : null}
      {draft.headers.length ? (
        <div className="flex gap-2">
          <dt className="shrink-0 font-medium text-gray-600 dark:text-gray-300">Headers</dt>
          <dd className="break-all">{draft.headers.join(', ')}</dd>
        </div>
      ) : null}
      {draft.secrets.map((secret) => (
        <div key={`${secret.target}:${secret.name}`} className="flex gap-2">
          <dt className="shrink-0 font-medium text-gray-600 dark:text-gray-300">Secret</dt>
          <dd className="break-all font-mono">
            {secret.name} → {secret.target} = {secret.value}
          </dd>
        </div>
      ))}
      {draft.paths.length ? (
        <div className="flex gap-2">
          <dt className="shrink-0 font-medium text-gray-600 dark:text-gray-300">Paths</dt>
          <dd className="break-all">{draft.paths.join(', ')}</dd>
        </div>
      ) : null}
      {draft.package ? (
        <div className="flex gap-2">
          <dt className="shrink-0 font-medium text-gray-600 dark:text-gray-300">Package</dt>
          <dd>
            {draft.package.runner} · {draft.package.ecosystem}
          </dd>
        </div>
      ) : null}
    </dl>
  );
}

/**
 * The import path: paste, look at what it would create, then save.
 *
 * The preview is a separate round trip rather than a client-side parse because
 * only the backend can say what a configuration means — which dialect it is,
 * which secrets it carries, and what would run. Everything the second call
 * needs (the text and the per-draft run locations) is kept here so the two
 * requests are provably about the same paste.
 */
export function McpImportStep({ onAdded }: { onAdded: (labels: string) => void }) {
  const [text, setText] = useState('');
  const [formatHint, setFormatHint] = useState('');
  const [preview, setPreview] = useState<McpImportPreviewApi | null>(null);
  const [locations, setLocations] = useState<Record<string, McpLocation>>({});
  const [busy, setBusy] = useState<'preview' | 'save' | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function runPreview() {
    setBusy('preview');
    setError(null);
    try {
      const result = await previewMcpImport(text, formatHint || undefined);
      setPreview(result);
      setLocations(Object.fromEntries(result.drafts.map((draft) => [draft.label, draft.location])));
    } catch (cause: unknown) {
      setPreview(null);
      setError(cause instanceof Error ? cause.message : 'Could not read that configuration.');
    } finally {
      setBusy(null);
    }
  }

  async function save() {
    setBusy('save');
    setError(null);
    try {
      const result = await saveMcpImport(text, locations);
      const labels = result.saved.map((connection) => connection.label).join(', ');
      onAdded(labels || 'Connection');
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not save the configuration.');
    } finally {
      setBusy(null);
    }
  }

  return (
    <div>
      <label
        className="block text-xs font-medium text-gray-700 dark:text-gray-300"
        htmlFor="mcp-import-text"
      >
        MCP configuration
      </label>
      <textarea
        id="mcp-import-text"
        value={text}
        onChange={(event) => {
          setText(event.target.value);
          // Any edit invalidates the preview: saving a paste the user has not
          // just seen is the one thing this step exists to prevent.
          setPreview(null);
        }}
        rows={8}
        placeholder='{"mcpServers": {"files": {"command": "npx", "args": ["-y", "server-filesystem"]}}}'
        className={`mt-1 font-mono ${FIELD}`}
      />
      <div className="mt-2 flex flex-wrap items-end gap-3">
        <div>
          <label
            className="block text-xs font-medium text-gray-700 dark:text-gray-300"
            htmlFor="mcp-import-format"
          >
            Format
          </label>
          <select
            id="mcp-import-format"
            value={formatHint}
            onChange={(event) => {
              setFormatHint(event.target.value);
              setPreview(null);
            }}
            className="mt-1 rounded-lg border border-gray-200 dark:border-white/10 bg-transparent px-2.5 py-2 text-sm text-gray-900 dark:text-gray-100"
          >
            <option value="">Detect automatically</option>
            <option value="json">JSON</option>
            <option value="toml">TOML</option>
          </select>
        </div>
        <button
          type="button"
          disabled={!text.trim() || busy !== null}
          onClick={() => void runPreview()}
          className="rounded-lg border border-gray-200 dark:border-white/10 px-3 py-1.5 text-sm font-medium text-gray-800 dark:text-gray-200 disabled:cursor-not-allowed disabled:opacity-60"
        >
          {busy === 'preview' ? 'Reading…' : 'Preview'}
        </button>
      </div>

      {error ? (
        <p role="alert" className="mt-3 text-sm text-red-600 dark:text-red-400">
          {error}
        </p>
      ) : null}

      {preview ? (
        <div className="mt-4">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            Read as {preview.dialect}. {preview.drafts.length}{' '}
            {preview.drafts.length === 1 ? 'server' : 'servers'} found.
          </p>
          {preview.drafts.map((draft) => (
            <div
              key={draft.label}
              className="mt-3 rounded-xl border border-gray-200 dark:border-white/10 p-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium text-gray-900 dark:text-gray-50">{draft.label}</span>
                <label className="flex items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
                  Run location
                  <select
                    aria-label={`Run location for ${draft.label}`}
                    value={locations[draft.label] ?? draft.location}
                    onChange={(event) =>
                      setLocations((current) => ({
                        ...current,
                        [draft.label]: event.target.value as McpLocation,
                      }))
                    }
                    className="rounded-lg border border-gray-200 dark:border-white/10 bg-transparent px-2 py-1 text-xs text-gray-900 dark:text-gray-100"
                  >
                    <option value="remote">Remote server</option>
                    <option value="managed-worker">Managed worker</option>
                    <option value="user-connector">Your connector</option>
                  </select>
                </label>
              </div>
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                {draft.transport} · imported as {draft.location}
              </p>
              <DraftBody draft={draft} />
              {draft.warnings.map((warning) => (
                <p
                  key={warning}
                  className="mt-2 rounded-lg bg-amber-50 dark:bg-amber-500/10 px-2 py-1 text-xs text-amber-800 dark:text-amber-300"
                >
                  {warning}
                </p>
              ))}
            </div>
          ))}
          {preview.rejected.length ? (
            <div className="mt-3 rounded-xl border border-amber-200 dark:border-amber-500/25 p-3">
              <h3 className="text-xs font-semibold text-amber-800 dark:text-amber-300">
                Not imported
              </h3>
              {preview.rejected.map((entry) => (
                <p key={entry.name} className="mt-1 text-xs text-amber-800 dark:text-amber-300">
                  {entry.name}: {entry.reason}
                </p>
              ))}
            </div>
          ) : null}
          <button
            type="button"
            disabled={preview.drafts.length === 0 || busy !== null}
            onClick={() => void save()}
            className="mt-4 rounded-lg bg-gray-900 dark:bg-gray-100 px-3 py-1.5 text-sm font-medium text-white dark:text-gray-900 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {busy === 'save'
              ? 'Saving…'
              : `Save ${preview.drafts.length} ${preview.drafts.length === 1 ? 'connection' : 'connections'}`}
          </button>
        </div>
      ) : null}
    </div>
  );
}
