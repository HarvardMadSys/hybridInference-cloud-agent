'use client';

import Link from 'next/link';
import { useT } from '@/components/providers';
import { SETTINGS_PATHS } from '@/components/features/settings/navigation';
import {
  MENU_DIVIDER,
  MENU_EMPTY,
  MENU_FOOTER_ACTION,
  MENU_ROW_STATIC,
  MENU_SCROLL,
  MENU_SURFACE,
} from '@/components/ui/menuStyles';
import { SkillSwitch } from '../skills/SkillDialog';
import type { SshChoice } from './useComposerSsh';

// Which of the owner's SSH servers this chat can reach. Nothing is on by
// default: registering a server and opening it to a chat are separate choices.

function ServerMark() {
  return (
    <span
      aria-hidden="true"
      className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md bg-surface-muted text-muted"
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
    <div className={`flex max-h-[320px] w-full flex-col overflow-hidden ${MENU_SURFACE}`}>
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
              {t('attach.sshRetry')}
            </button>
          </div>
        ) : choices.length === 0 ? (
          <p className={MENU_EMPTY}>{t('attach.sshEmpty')}</p>
        ) : (
          <ul>
            {choices.map((choice) => (
              <li key={choice.id} className={MENU_ROW_STATIC}>
                <ServerMark />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13px] font-medium text-foreground">
                    {choice.name}
                  </span>
                  <span
                    className={`block truncate text-[11.5px] ${
                      choice.blocked ? 'text-status-warning' : 'text-muted'
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
      <div className={`${MENU_DIVIDER} m-0`} role="presentation" />
      <div className="p-1.5">
        <Link href={SETTINGS_PATHS.ssh} className={MENU_FOOTER_ACTION}>
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
