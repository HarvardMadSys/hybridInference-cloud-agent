'use client';

import Link from 'next/link';
import { useMemo, useState } from 'react';
import { useT } from '@/components/providers';
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
      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-gray-100 text-[11px] font-semibold uppercase text-gray-500 dark:bg-white/10 dark:text-gray-300"
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
}: {
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
    <div className="flex max-h-[320px] w-[280px] flex-col overflow-hidden rounded-xl border border-gray-200 bg-white shadow-lg dark:border-white/10 dark:bg-gray-900">
      <div className="border-b border-gray-100 p-2 dark:border-white/5">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          aria-label={t('attach.mcpSearch')}
          placeholder={t('attach.mcpSearch')}
          className="w-full rounded-lg bg-transparent px-2 py-1.5 text-[13px] text-gray-900 placeholder:text-gray-400 focus:outline-none dark:text-gray-100 dark:placeholder:text-gray-500"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-1.5">
        {loading ? (
          <p role="status" className="px-2 py-3 text-[13px] text-gray-500 dark:text-gray-400">
            {t('common.loading')}
          </p>
        ) : error ? (
          <div className="px-2 py-3">
            <p className="text-[13px] text-red-600 dark:text-red-400">{error}</p>
            <button
              type="button"
              onClick={onRetry}
              className="mt-1.5 text-[12.5px] text-gray-500 underline dark:text-gray-400"
            >
              {t('attach.mcpRetry')}
            </button>
          </div>
        ) : shown.length === 0 ? (
          <p className="px-2 py-3 text-[13px] text-gray-500 dark:text-gray-400">
            {choices.length === 0 ? t('attach.mcpEmpty') : t('attach.mcpNoMatch')}
          </p>
        ) : (
          <ul>
            {shown.map((choice) => (
              <li
                key={choice.connection.id}
                className="flex items-center gap-2.5 rounded-lg px-2 py-2"
              >
                <ServerMark label={choice.connection.label} />
                <span className="min-w-0 flex-1">
                  <span
                    className="block truncate text-[13px] font-medium text-gray-900 dark:text-gray-100"
                    title={choice.connection.label}
                  >
                    {choice.connection.label}
                  </span>
                  <span
                    className={`block truncate text-[11.5px] ${
                      choice.error
                        ? 'text-amber-700 dark:text-amber-400'
                        : 'text-gray-500 dark:text-gray-400'
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

      <div className="border-t border-gray-100 p-1.5 dark:border-white/5">
        <Link
          href="/integrations"
          className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-[13px] text-gray-700 hover:bg-gray-50 dark:text-gray-200 dark:hover:bg-white/5"
        >
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
