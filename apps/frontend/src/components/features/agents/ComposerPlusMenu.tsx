'use client';

import { useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { SkillIcon } from './skills/SkillDialog';
import { McpSubmenu } from './mcp/McpSubmenu';
import type { McpChoice } from './mcp/useComposerMcp';

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

const ITEM =
  'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13.5px] font-medium text-gray-900 hover:bg-gray-50 focus:bg-gray-50 focus:outline-none dark:text-gray-100 dark:hover:bg-white/5 dark:focus:bg-white/5';

export function ComposerPlusMenu({
  onFiles,
  onOpenSkills,
  skillSummary,
  goalSummary,
  onOpenGoal,
  mcp,
  mcpBlockedReason,
  onOpen,
  disabled = false,
}: {
  /**
   * Absent on a composer that offers neither. Both are optional because the
   * attach point is one menu with one job — holding what this message carries —
   * and a caller that can only offer part of that should show the part it has
   * rather than grow a second, differently-shaped menu beside it.
   */
  onFiles?: (files: File[]) => void;
  onOpenSkills: () => void;
  /** What the Skills row reports on its right: a count, or a pending note. */
  skillSummary: string;
  /** What the completion-condition row shows under its label. */
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
  /** Set when the chosen runtime cannot be given MCP connections at all. */
  mcpBlockedReason?: string;
  onOpen?: () => void;
  disabled?: boolean;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [submenu, setSubmenu] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const filePicker = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    menu.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    function onPointerDown(event: MouseEvent) {
      const target = event.target as Node;
      if (menu.current?.contains(target) || trigger.current?.contains(target)) return;
      setOpen(false);
    }
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open]);

  function close(restoreFocus = true) {
    setOpen(false);
    setSubmenu(false);
    if (restoreFocus) trigger.current?.focus();
  }

  function moveFocus(step: 1 | -1) {
    const items = Array.from(
      menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [],
    );
    if (items.length === 0) return;
    const at = items.indexOf(document.activeElement as HTMLElement);
    const next = at < 0 ? 0 : (at + step + items.length) % items.length;
    items[next]?.focus();
  }

  function take(event: React.ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(event.target.files ?? []);
    // Clearing lets the same file be picked again — without it the input's
    // value is unchanged and no second `change` event fires.
    event.target.value = '';
    if (picked.length) onFiles?.(picked);
  }

  return (
    <>
      <button
        ref={trigger}
        type="button"
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t('attach.open')}
        title={t('attach.open')}
        onClick={() => {
          if (!open) onOpen?.();
          setOpen((was) => !was);
        }}
        onKeyDown={(event) => {
          if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
          event.preventDefault();
          if (!open) onOpen?.();
          setOpen(true);
        }}
        // Borderless, like the pickers beside it. A boxed button here was a
        // third visual language in a row that already has two: unbordered
        // controls on the left and one solid button on the right.
        className={`inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
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
          role="menu"
          aria-label={t('attach.open')}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault();
              close();
            } else if (event.key === 'ArrowDown') {
              event.preventDefault();
              moveFocus(1);
            } else if (event.key === 'ArrowUp') {
              event.preventDefault();
              moveFocus(-1);
            }
          }}
          className="absolute bottom-[calc(100%+6px)] left-0 z-50 w-[272px] rounded-xl border border-gray-200 bg-white p-1.5 shadow-lg dark:border-white/10 dark:bg-gray-900"
        >
          {onFiles ? (
            <>
              <button
                type="button"
                role="menuitem"
                className={ITEM}
                onClick={() => {
                  close(false);
                  filePicker.current?.click();
                }}
              >
                <PaperclipIcon />
                {t('attach.upload')}
              </button>
              <div className="my-1 h-px bg-gray-100 dark:bg-white/5" />
            </>
          ) : null}
          <button
            type="button"
            role="menuitem"
            className={ITEM}
            onClick={() => {
              close(false);
              onOpenSkills();
            }}
          >
            <SkillIcon className="h-[17px] w-[17px] shrink-0" />
            <span className="flex-1">{t('attach.skills')}</span>
            <span className="text-xs font-normal text-gray-500 dark:text-gray-400">
              {skillSummary}
            </span>
            <ChevronIcon />
          </button>

          {/* Its own group. Skills and MCP connections are things this
              message *carries*; the completion condition is what the whole
              conversation is for, and it outlives every message after it. */}
          <div className="my-1 h-px bg-gray-100 dark:bg-white/5" />
          <button
            type="button"
            role="menuitem"
            className={ITEM}
            onClick={() => {
              close(false);
              onOpenGoal();
            }}
          >
            <GoalIcon />
            <span className="flex-1">
              {t('attach.goal')}
              <span className="mt-0.5 block truncate text-xs font-normal text-gray-500 dark:text-gray-400">
                {goalSummary}
              </span>
            </span>
            <ChevronIcon />
          </button>

          {mcp ? (
            <>
              <button
                type="button"
                role="menuitem"
                aria-haspopup="menu"
                aria-expanded={submenu}
                aria-disabled={mcpBlockedReason ? true : undefined}
                className={ITEM}
                onClick={() => {
                  if (mcpBlockedReason) return;
                  setSubmenu((was) => !was);
                }}
              >
                <McpIcon />
                <span className="flex-1">
                  {t('attach.mcp')}
                  {mcpBlockedReason ? (
                    // Said here rather than at job creation: the API refuses this
                    // pairing with a 400, and learning that after pressing Run is
                    // learning it too late.
                    <span className="mt-0.5 block text-xs font-normal text-gray-500 dark:text-gray-400">
                      {mcpBlockedReason}
                    </span>
                  ) : null}
                </span>
                {!mcpBlockedReason && mcp.count ? (
                  <span className="text-xs font-normal text-gray-500 dark:text-gray-400">
                    {mcp.count}
                  </span>
                ) : null}
                {mcpBlockedReason ? null : <ChevronIcon />}
              </button>
            </>
          ) : null}

          {mcp && submenu && !mcpBlockedReason ? (
            <div className="absolute bottom-0 left-full ml-1.5">
              <McpSubmenu
                choices={mcp.choices}
                loading={mcp.loading}
                error={mcp.error}
                onRetry={mcp.reload}
                onToggle={(choice, on) => mcp.toggle(choice.connection, on)}
              />
            </div>
          ) : null}
        </div>
      ) : null}
    </>
  );
}
