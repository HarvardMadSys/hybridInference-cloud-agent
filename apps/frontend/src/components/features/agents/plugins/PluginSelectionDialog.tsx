'use client';

import { useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers';
import { useModalDialog } from '@/components/ui/useModalDialog';
import type { AgentPluginInputApi } from '@/lib/api/agents';
import {
  MAX_PLUGINS,
  MAX_PLUGIN_LENGTH,
  MAX_MARKETPLACE_LENGTH,
  marketplacePluginRuntime,
  normalizeNpmPlugin,
  npmPluginIdentity,
  validNpmPlugin,
  validPluginMarketplace,
  validPluginName,
} from '@/lib/pluginConfiguration';

export function PluginSelectionDialog({
  selected,
  onChange,
  onClose,
  blockedReason,
  runtime,
}: {
  selected: AgentPluginInputApi[];
  onChange: (plugins: AgentPluginInputApi[]) => void;
  onClose: () => void;
  blockedReason?: string;
  runtime: string;
}) {
  const t = useT();
  const id = useId();
  const panel = useRef<HTMLDivElement>(null);
  const [plugin, setPlugin] = useState('');
  const [marketplace, setMarketplace] = useState('');
  const [error, setError] = useState<string | null>(null);
  const nativeMarketplace = marketplacePluginRuntime(runtime);
  useModalDialog(panel, { onClose, initialFocus: 'input' });

  function add(event: React.FormEvent) {
    event.preventDefault();
    if (blockedReason) return;
    const rawName = plugin.trim();
    const input: AgentPluginInputApi = nativeMarketplace
      ? { plugin: rawName, marketplace: marketplace.trim() }
      : { plugin: normalizeNpmPlugin(rawName) };
    if (nativeMarketplace ? !validPluginName(rawName, runtime) : !validNpmPlugin(rawName)) {
      setError(
        t(
          nativeMarketplace
            ? runtime === 'codex'
              ? 'plugins.invalidCodexName'
              : 'plugins.invalidName'
            : 'plugins.invalidPackage',
        ),
      );
      return;
    }
    if (nativeMarketplace && !validPluginMarketplace(input.marketplace ?? '')) {
      setError(t('plugins.invalidMarketplace'));
      return;
    }
    if (
      selected.some((existing) =>
        nativeMarketplace
          ? existing.plugin === input.plugin
          : npmPluginIdentity(existing.plugin) === npmPluginIdentity(input.plugin),
      )
    ) {
      setError(t('plugins.duplicate'));
      return;
    }
    if (
      nativeMarketplace &&
      selected.some(
        (existing) =>
          existing.plugin.split('@')[1] === input.plugin.split('@')[1] &&
          existing.marketplace !== input.marketplace,
      )
    ) {
      setError(t('plugins.sourceConflict'));
      return;
    }
    if (selected.length >= MAX_PLUGINS) {
      setError(t('plugins.limit', { limit: MAX_PLUGINS }));
      return;
    }
    onChange([...selected, input]);
    setPlugin('');
    setMarketplace('');
    setError(null);
    panel.current?.querySelector('input')?.focus();
  }

  if (typeof document === 'undefined') return null;
  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
      <button
        type="button"
        tabIndex={-1}
        aria-label={t('plugins.dismiss')}
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-gray-900/25 backdrop-blur-[2px] dark:bg-black/50"
      />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={`${id}-title`}
        aria-describedby={`${id}-description`}
        className="relative flex max-h-full w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-border bg-surface shadow-card"
      >
        <div className="shrink-0 px-6 pb-4 pt-5">
          <h2 id={`${id}-title`} className="text-lg font-semibold text-foreground">
            {t('plugins.title')}
          </h2>
          <p id={`${id}-description`} className="mt-1 text-sm text-muted">
            {t('plugins.description', { runtime })}
          </p>
        </div>
        <div className="min-h-0 overflow-y-auto px-6 pb-5">
          {blockedReason ? (
            <p role="alert" className="mb-4 text-sm text-status-error">
              {blockedReason}
            </p>
          ) : null}
          {selected.length ? (
            <ul className="mb-5 space-y-3">
              {selected.map((item) => (
                <li
                  key={item.plugin}
                  className="flex items-start gap-3 rounded-lg border border-border p-3"
                >
                  <div className="min-w-0 flex-1">
                    <p className="break-words text-sm font-medium text-foreground">{item.plugin}</p>
                    {item.marketplace ? (
                      <p className="mt-1 break-all text-xs text-muted">{item.marketplace}</p>
                    ) : null}
                  </div>
                  <button
                    type="button"
                    className="ui-button ui-button-secondary shrink-0"
                    aria-label={t('plugins.removeNamed', { name: item.plugin })}
                    onClick={() => {
                      onChange(selected.filter((existing) => existing.plugin !== item.plugin));
                      setError(null);
                    }}
                  >
                    {t('plugins.remove')}
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="mb-5 text-sm text-muted">{t('plugins.empty')}</p>
          )}
          <form onSubmit={add} className="space-y-4">
            <label className="block text-sm font-medium text-foreground" htmlFor={`${id}-plugin`}>
              {t(nativeMarketplace ? 'plugins.plugin' : 'plugins.package')}
              <input
                id={`${id}-plugin`}
                value={plugin}
                onChange={(event) => setPlugin(event.target.value)}
                placeholder={
                  runtime === 'codex'
                    ? 'review@my-marketplace'
                    : nativeMarketplace
                      ? 'code-review@claude-plugins-official'
                      : '@scope/plugin@1.2.3'
                }
                maxLength={MAX_PLUGIN_LENGTH}
                disabled={!!blockedReason}
                autoComplete="off"
                spellCheck={false}
                className="ui-focus-ring mt-1 w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm disabled:opacity-50"
              />
              {nativeMarketplace ? null : (
                <span className="mt-1 block text-xs font-normal text-muted">
                  {t('plugins.packageHint')}
                </span>
              )}
            </label>
            {nativeMarketplace ? (
              <label
                className="block text-sm font-medium text-foreground"
                htmlFor={`${id}-marketplace`}
              >
                {t('plugins.marketplace')}
                <input
                  id={`${id}-marketplace`}
                  value={marketplace}
                  onChange={(event) => setMarketplace(event.target.value)}
                  placeholder={
                    runtime === 'codex'
                      ? 'your-org/codex-plugins'
                      : 'anthropics/claude-plugins-official'
                  }
                  maxLength={MAX_MARKETPLACE_LENGTH}
                  disabled={!!blockedReason}
                  autoComplete="off"
                  spellCheck={false}
                  className="ui-focus-ring mt-1 w-full rounded-lg border border-border bg-transparent px-3 py-2 text-sm disabled:opacity-50"
                />
                <span className="mt-1 block text-xs font-normal text-muted">
                  {t('plugins.marketplaceHint')}
                </span>
              </label>
            ) : null}
            {error ? (
              <p role="alert" className="text-sm text-status-error">
                {error}
              </p>
            ) : null}
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="submit"
                disabled={!!blockedReason || selected.length >= MAX_PLUGINS}
                className="ui-button ui-button-secondary"
              >
                {t('plugins.add')}
              </button>
              <p className="text-xs text-muted">{t('plugins.limit', { limit: MAX_PLUGINS })}</p>
            </div>
          </form>
        </div>
        <div className="flex shrink-0 justify-end border-t border-border px-6 py-4">
          <button type="button" onClick={onClose} className="ui-button ui-button-primary">
            {t('plugins.done')}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
