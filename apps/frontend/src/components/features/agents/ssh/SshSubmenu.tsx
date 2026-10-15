'use client';

import Link from 'next/link';
import { useT } from '@/components/providers';
import { SETTINGS_PATHS } from '@/components/features/settings/navigation';
import { SkillSwitch } from '../skills/SkillDialog';
import type { SshChoice } from './useComposerSsh';

// Which of the owner's SSH servers this chat can reach. Nothing is on by
// default: registering a server and opening it to a chat are separate choices.

function ServerMark() {
  return (
    <span
      aria-hidden="true"
      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-gray-100 text-gray-500 dark:bg-white/10 dark:text-gray-300"
    >
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.8}
        className="h-3.5 w-3.5"
      >
        <path strokeLinecap="round" strokeLinejoin="round" d="m5 8 4 4-4 4m6 0h8" />
      </svg>
    </span>
  );
}

export function SshSubmenu({
  choices,
  loading,
  saving,
  error,
  onToggle,
  onRetry,
}: {
  choices: SshChoice[];
  loading: boolean;
  saving: boolean;
  error: string | null;
  onToggle: (choice: SshChoice, on: boolean) => void;
  onRetry: () => void;
}) {
  const t = useT();
  return (
    <div className="flex max-h-[320px] w-full flex-col overflow-hidden">
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
              {t('attach.sshRetry')}
            </button>
          </div>
        ) : choices.length === 0 ? (
          <p className="px-2 py-3 text-[13px] text-gray-500 dark:text-gray-400">
            {t('attach.sshEmpty')}
          </p>
        ) : (
          <ul>
            {choices.map((choice) => (
              <li key={choice.id} className="flex items-center gap-2.5 rounded-lg px-2 py-2">
                <ServerMark />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-medium text-gray-900 dark:text-gray-100">
                    {choice.name}
                  </span>
                  <span
                    className={`block truncate text-[11.5px] ${
                      choice.blocked
                        ? 'text-amber-700 dark:text-amber-400'
                        : 'text-gray-500 dark:text-gray-400'
                    }`}
                  >
                    {choice.blocked ? t('attach.sshNotChecked') : choice.login}
                  </span>
                </span>
                {choice.blocked ? null : (
                  <SkillSwitch
                    label={choice.name}
                    checked={choice.selected}
                    disabled={saving}
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
          href={SETTINGS_PATHS.ssh}
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
          {t('attach.sshManage')}
        </Link>
      </div>
    </div>
  );
}
