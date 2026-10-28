'use client';

import Link from 'next/link';
import { useMemo, useState } from 'react';
import { useT } from '@/components/providers';
import {
  MENU_DIVIDER,
  MENU_EMPTY,
  MENU_FOOTER_ACTION,
  MENU_ROW,
  MENU_SCROLL,
  MENU_SEARCH,
  MENU_SEARCH_INPUT,
  MENU_SURFACE,
} from '@/components/ui/menuStyles';
import { SkillSwitch } from '../skills/SkillDialog';
import type { McpChoice } from './useComposerMcp';

// Which servers this one task may call, chosen the same way the servers
// themselves are listed elsewhere: by label, with a switch.
//
// Connecting a server and letting a task call it are two separate decisions,
// which is why nothing here is on by default — the control plane reads an
// omitted selection as none rather than as "all of them".

function ServerMark({ label }: { label: string }) {
  return (
    <span
      aria-hidden="true"
      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-surface-muted text-[11px] font-semibold uppercase text-muted"
    >
      {label.trim().slice(0, 1) || '?'}
    </span>
  );
}

export function McpSubmenu({
  choices,
  loading,
  error,
  onToggle,
  onRetry,
  embedded = false,
}: {
  embedded?: boolean;
  choices: McpChoice[];
  loading: boolean;
  error: string | null;
  onToggle: (choice: McpChoice, on: boolean) => void;
  onRetry: () => void;
}) {
  const t = useT();
  const [query, setQuery] = useState('');
  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return choices;
    return choices.filter((choice) => choice.connection.label.toLowerCase().includes(needle));
  }, [choices, query]);

  return (
    <div
      className={`flex max-h-[320px] flex-col overflow-hidden ${embedded ? 'w-full' : `w-[280px] ${MENU_SURFACE}`}`}
    >
      <div className={`${MENU_SEARCH} !m-0 rounded-none border-0 border-b border-border`}>
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label={t('attach.mcpSearch')}
          placeholder={t('attach.mcpSearch')}
          className={MENU_SEARCH_INPUT}
        />
      </div>

      <div className={MENU_SCROLL}>
        {loading ? (
          <p role="status" className={MENU_EMPTY}>
            {t('common.loading')}
          </p>
        ) : error ? (
          <div className="px-2 py-3">
            <p className="text-[13px] text-status-error">{error}</p>
            <button
              type="button"
              onClick={onRetry}
              className="mt-1.5 text-[12.5px] text-muted underline"
            >
              {t('attach.mcpRetry')}
            </button>
          </div>
        ) : shown.length === 0 ? (
          <p className={MENU_EMPTY}>
            {choices.length === 0 ? t('attach.mcpEmpty') : t('attach.mcpNoMatch')}
          </p>
        ) : (
          <ul>
            {shown.map((choice) => (
              <li key={choice.connection.id} className={`${MENU_ROW} cursor-default`}>
                <ServerMark label={choice.connection.label} />
                <span className="min-w-0 flex-1">
                  <span
                    className="block truncate text-[13px] font-medium text-foreground"
                    title={choice.connection.label}
                  >
                    {choice.connection.label}
                  </span>
                  <span
                    className={`block truncate text-[11.5px] ${
                      choice.error ? 'text-status-warning' : 'text-muted'
                    }`}
                  >
                    {choice.busy
                      ? t('common.loading')
                      : (choice.error ?? t('attach.mcpTools', { count: choice.toolCount }))}
                  </span>
                </span>
                {choice.blocked ? null : (
                  <SkillSwitch
                    label={choice.connection.label}
                    checked={choice.selected}
                    disabled={choice.busy}
                    onChange={(on) => onToggle(choice, on)}
                  />
                )}
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className={`${MENU_DIVIDER} m-0`} role="presentation" />
      <div className="p-1.5">
        <Link href="/settings/plugins" className={MENU_FOOTER_ACTION}>
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.8}
            aria-hidden="true"
            className="h-4 w-4"
          >
            <path strokeLinecap="round" d="M12 5v14M5 12h14" />
          </svg>
          {t('attach.mcpAdd')}
        </Link>
      </div>
    </div>
  );
}
