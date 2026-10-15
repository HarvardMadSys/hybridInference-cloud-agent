'use client';

import { useEffect, useId, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { SkillIcon } from './skills/SkillDialog';
import { McpSubmenu } from './mcp/McpSubmenu';
import { SshSubmenu } from './ssh/SshSubmenu';
import type { ComposerSsh } from './ssh/useComposerSsh';
import { useComposerMenuLayout } from './useComposerMenuLayout';
import type { McpChoice } from './mcp/useComposerMcp';
import {
  COMPOSER_MENU_SURFACE,
  COMPOSER_MENU_HEADING,
  COMPOSER_MENU_ROW,
  COMPOSER_MENU_DETAIL,
} from './composerMenuStyles';

// The composer's one attach point.
//
// The control row used to carry Skills next to the repository, branch, agent,
// model and reasoning pickers. Those five each take exactly one value and are
// always meaningful, so they are always visible. Skills is zero-or-more and
// most tasks carry none — a different kind of choice wearing the same kind of
// control. This menu is where that second kind lives.
//
// The upload item opens the operating system's own file picker rather than a
// dialog of our own. A picker we drew would be a second thing to learn, a
// second thing to make accessible, and a second thing to keep working; the
// native one is already all three.
//
// One item, not two. A file input chooses files *or* a directory, never both —
// the directory variant needs `webkitdirectory`, which turns the dialog into a
// folder-only chooser — so offering both as clickable items meant two rows for
// what reads as one action. Files are the common case and get the click;
// a folder is dropped onto the composer, which takes files and folders in the
// same gesture.

function PlusIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      aria-hidden="true"
      className="h-[15px] w-[15px]"
    >
      <path strokeLinecap="round" d="M12 5v14M5 12h14" />
    </svg>
  );
}

function McpIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden="true"
      className="h-[17px] w-[17px] shrink-0"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9 3v4m6-4v4M5 7h14v5a7 7 0 0 1-14 0V7Zm7 12v2"
      />
    </svg>
  );
}

function SshIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden="true"
      className="h-[17px] w-[17px] shrink-0"
    >
      <rect x="3" y="4" width="18" height="7" rx="2" />
      <rect x="3" y="13" width="18" height="7" rx="2" />
      <path strokeLinecap="round" d="M7 7.5h.01M7 16.5h.01" />
    </svg>
  );
}

function EnvironmentIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden="true"
      className="h-[17px] w-[17px] shrink-0"
    >
      <rect x="3" y="5" width="18" height="14" rx="3" />
      <path strokeLinecap="round" strokeLinejoin="round" d="m7 9 3 3-3 3m6 0h4" />
    </svg>
  );
}

export function GoalIcon({ className = 'h-[17px] w-[17px] shrink-0' }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.7}
      aria-hidden="true"
      className={className}
    >
      <circle cx="12" cy="12" r="8.5" />
      <circle cx="12" cy="12" r="3.5" />
      <path strokeLinecap="round" d="M12 3.5v3M12 17.5v3M3.5 12h3M17.5 12h3" />
    </svg>
  );
}

function ChevronIcon() {
  return (
    <svg
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden="true"
      className="h-3.5 w-3.5 shrink-0 text-gray-400 dark:text-gray-500"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="m8 6 4 4-4 4" />
    </svg>
  );
}

function PaperclipIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden="true"
      className="h-[17px] w-[17px] shrink-0"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M21.4 11.1 12.3 20a5.5 5.5 0 0 1-7.8-7.8l9.2-9.1a3.7 3.7 0 0 1 5.2 5.2l-9.2 9.1a1.8 1.8 0 0 1-2.6-2.6l8.5-8.4"
      />
    </svg>
  );
}

export function ComposerPlusMenu({
  onFiles,
  onOpenSkills,
  skillSummary,
  goalSummary,
  onOpenGoal,
  mcp,
  mcpBlockedReason,
  ssh,
  onOpenPlugins,
  pluginSummary,
  onOpenEnvironment,
  environmentSummary,
  onOpen,
  disabled = false,
}: {
  onFiles?: (files: File[]) => void;
  onOpenSkills: () => void;
  /** The number selected, or a note while defaults load. */
  skillSummary: string;
  /** The description beside Goal, shared with the slash-command menu. */
  goalSummary: string;
  onOpenGoal: () => void;
  mcp?: {
    choices: McpChoice[];
    loading: boolean;
    error: string | null;
    count: number;
    reload: () => void;
    toggle: (connection: McpChoice['connection'], on: boolean) => void;
  };
  mcpBlockedReason?: string;
  /** The owner's SSH servers this chat can reach; absent hides the row. */
  ssh?: ComposerSsh;
  onOpenPlugins?: () => void;
  pluginSummary?: string;
  onOpenEnvironment?: () => void;
  environmentSummary?: string;
  onOpen?: () => void;
  disabled?: boolean;
}) {
  const t = useT();
  const id = useId();
  const [open, setOpen] = useState(false);
  const [submenu, setSubmenu] = useState<'mcp' | 'ssh' | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const mcpTrigger = useRef<HTMLButtonElement>(null);
  const sshTrigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const filePicker = useRef<HTMLInputElement>(null);
  const wasSubmenu = useRef<'mcp' | 'ssh' | null>(null);
  const openAtEnd = useRef(false);
  const menuStyle = useComposerMenuLayout(open, menu);

  useEffect(() => {
    if (!open) {
      wasSubmenu.current = null;
      return;
    }
    if (submenu) {
      (
        menu.current?.querySelector<HTMLElement>('input[type="search"]') ??
        menu.current?.querySelector<HTMLElement>('[role="switch"]') ??
        menu.current?.querySelector<HTMLElement>('button')
      )?.focus();
    } else if (wasSubmenu.current) {
      (wasSubmenu.current === 'ssh' ? sshTrigger : mcpTrigger).current?.focus();
    } else {
      const items = menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]');
      items?.[openAtEnd.current ? items.length - 1 : 0]?.focus();
    }
    wasSubmenu.current = submenu;
  }, [open, submenu]);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (menu.current?.contains(target) || trigger.current?.contains(target)) return;
      setOpen(false);
      setSubmenu(null);
    }
    document.addEventListener('pointerdown', onPointerDown);
    return () => document.removeEventListener('pointerdown', onPointerDown);
  }, [open]);

  function close(restoreFocus = true) {
    setOpen(false);
    setSubmenu(null);
    if (restoreFocus) trigger.current?.focus();
  }

  function moveFocus(key: string) {
    const items = Array.from(
      menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [],
    );
    if (!items.length) return;
    const at = items.indexOf(document.activeElement as HTMLElement);
    const next =
      key === 'Home'
        ? 0
        : key === 'End'
          ? items.length - 1
          : (at + (key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
    items[next]?.focus();
  }

  function take(event: React.ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(event.target.files ?? []);
    event.target.value = '';
    if (picked.length) onFiles?.(picked);
  }

  return (
    <>
      <button
        ref={trigger}
        type="button"
        disabled={disabled}
        aria-haspopup={submenu ? 'dialog' : 'menu'}
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-label={t('attach.open')}
        title={t('attach.open')}
        onClick={() => {
          if (open) close();
          else {
            openAtEnd.current = false;
            onOpen?.();
            setOpen(true);
          }
        }}
        onKeyDown={(event) => {
          if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
          event.preventDefault();
          openAtEnd.current = event.key === 'ArrowUp';
          if (!open) onOpen?.();
          setOpen(true);
        }}
        className={`ui-focus-ring inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors disabled:cursor-not-allowed disabled:opacity-50 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11 ${
          open
            ? 'bg-gray-100 text-gray-900 dark:bg-white/10 dark:text-gray-100'
            : 'text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-200'
        }`}
      >
        <PlusIcon />
      </button>
      {onFiles ? (
        <input
          ref={filePicker}
          type="file"
          multiple
          hidden
          aria-hidden="true"
          tabIndex={-1}
          onChange={take}
        />
      ) : null}
      {open ? (
        <div
          ref={menu}
          id={id}
          role={submenu ? 'dialog' : 'menu'}
          aria-label={t(
            submenu === 'ssh' ? 'attach.ssh' : submenu === 'mcp' ? 'attach.mcp' : 'attach.open',
          )}
          className={COMPOSER_MENU_SURFACE}
          style={menuStyle}
          onBlur={(event) => {
            if (
              event.relatedTarget &&
              !event.currentTarget.contains(event.relatedTarget as Node) &&
              !trigger.current?.contains(event.relatedTarget as Node)
            )
              close(false);
          }}
          onKeyDown={(event) => {
            if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
            if (event.key === 'Escape') {
              event.preventDefault();
              event.stopPropagation();
              if (submenu) setSubmenu(null);
              else close();
            } else if (!submenu && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
              event.preventDefault();
              moveFocus(event.key);
            }
          }}
        >
          {submenu === 'ssh' && ssh ? (
            <>
              <button type="button" className={COMPOSER_MENU_ROW} onClick={() => setSubmenu(null)}>
                <span className="rotate-180">
                  <ChevronIcon />
                </span>
                {t('attach.back')}
                <span className={COMPOSER_MENU_DETAIL}>{t('attach.ssh')}</span>
              </button>
              <SshSubmenu
                choices={ssh.choices}
                loading={ssh.loading}
                saving={ssh.saving}
                error={ssh.error}
                onRetry={ssh.reload}
                onToggle={(choice, on) => ssh.toggle(choice.id, on)}
              />
            </>
          ) : submenu === 'mcp' && mcp ? (
            <>
              <button type="button" className={COMPOSER_MENU_ROW} onClick={() => setSubmenu(null)}>
                <span className="rotate-180">
                  <ChevronIcon />
                </span>
                {t('attach.back')}
                <span className={COMPOSER_MENU_DETAIL}>{t('attach.mcp')}</span>
              </button>
              <McpSubmenu
                embedded
                choices={mcp.choices}
                loading={mcp.loading}
                error={mcp.error}
                onRetry={mcp.reload}
                onToggle={(choice, on) => mcp.toggle(choice.connection, on)}
              />
            </>
          ) : (
            <>
              <div role="group" aria-label={t('attach.add')}>
                <p className={COMPOSER_MENU_HEADING}>{t('attach.add')}</p>
                {onFiles ? (
                  <button
                    type="button"
                    role="menuitem"
                    tabIndex={-1}
                    aria-label={t('attach.upload')}
                    className={COMPOSER_MENU_ROW}
                    onClick={() => {
                      close(false);
                      filePicker.current?.click();
                    }}
                  >
                    <PaperclipIcon />
                    <span className="shrink-0">{t('attach.file')}</span>
                  </button>
                ) : null}
                <button
                  type="button"
                  role="menuitem"
                  tabIndex={-1}
                  className={COMPOSER_MENU_ROW}
                  onClick={() => {
                    close(false);
                    onOpenGoal();
                  }}
                >
                  <GoalIcon />
                  <span className="shrink-0">{t('attach.goal')}</span>
                  <span title={goalSummary} className={COMPOSER_MENU_DETAIL}>
                    {goalSummary}
                  </span>
                </button>
              </div>
              <div role="group" aria-label={t('attach.tools')} className="mt-2">
                <p className={COMPOSER_MENU_HEADING}>{t('attach.tools')}</p>
                <button
                  type="button"
                  role="menuitem"
                  tabIndex={-1}
                  className={COMPOSER_MENU_ROW}
                  onClick={() => {
                    close(false);
                    onOpenSkills();
                  }}
                >
                  <SkillIcon className="h-[17px] w-[17px] shrink-0" />
                  <span className="shrink-0">{t('attach.skills')}</span>
                  <span className={COMPOSER_MENU_DETAIL}>{skillSummary}</span>
                  <ChevronIcon />
                </button>
                {mcp ? (
                  <button
                    ref={mcpTrigger}
                    type="button"
                    role="menuitem"
                    tabIndex={-1}
                    aria-haspopup="dialog"
                    aria-disabled={mcpBlockedReason ? true : undefined}
                    className={COMPOSER_MENU_ROW}
                    onClick={() => {
                      if (!mcpBlockedReason) setSubmenu('mcp');
                    }}
                  >
                    <McpIcon />
                    <span className="shrink-0">{t('attach.mcp')}</span>
                    <span className={COMPOSER_MENU_DETAIL} title={mcpBlockedReason}>
                      {mcpBlockedReason ?? (mcp.count ? mcp.count : '')}
                    </span>
                    {mcpBlockedReason ? null : <ChevronIcon />}
                  </button>
                ) : null}
                {ssh ? (
                  <button
                    ref={sshTrigger}
                    type="button"
                    role="menuitem"
                    tabIndex={-1}
                    aria-haspopup="dialog"
                    className={COMPOSER_MENU_ROW}
                    onClick={() => {
                      ssh.reload();
                      setSubmenu('ssh');
                    }}
                  >
                    <SshIcon />
                    <span className="shrink-0">{t('attach.ssh')}</span>
                    <span className={COMPOSER_MENU_DETAIL}>{ssh.count ? ssh.count : ''}</span>
                    <ChevronIcon />
                  </button>
                ) : null}
                {onOpenPlugins ? (
                  <button
                    type="button"
                    role="menuitem"
                    tabIndex={-1}
                    aria-haspopup="dialog"
                    className={COMPOSER_MENU_ROW}
                    onClick={() => {
                      close();
                      onOpenPlugins();
                    }}
                  >
                    <McpIcon />
                    <span className="shrink-0">{t('plugins.title')}</span>
                    <span className={COMPOSER_MENU_DETAIL}>{pluginSummary}</span>
                    <ChevronIcon />
                  </button>
                ) : null}
              </div>
              {onOpenEnvironment ? (
                <div role="group" aria-label={t('attach.workspace')} className="mt-2">
                  <p className={COMPOSER_MENU_HEADING}>{t('attach.workspace')}</p>
                  <button
                    type="button"
                    role="menuitem"
                    tabIndex={-1}
                    aria-haspopup="dialog"
                    className={COMPOSER_MENU_ROW}
                    onClick={() => {
                      close();
                      onOpenEnvironment();
                    }}
                  >
                    <EnvironmentIcon />
                    <span className="shrink-0">{t('environment.title')}</span>
                    <span className={COMPOSER_MENU_DETAIL} title={environmentSummary}>
                      {environmentSummary}
                    </span>
                    <ChevronIcon />
                  </button>
                </div>
              ) : null}
            </>
          )}
        </div>
      ) : null}
    </>
  );
}
