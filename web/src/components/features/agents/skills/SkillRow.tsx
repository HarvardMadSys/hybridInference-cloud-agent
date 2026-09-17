'use client';

import { useEffect, useId, useRef, useState } from 'react';
import type { SkillSummaryApi } from '@/lib/api/skills';
import { SkillIcon, SkillSwitch } from './SkillDialog';

export const SOURCE_LABELS = {
  created: 'Created by you',
  uploaded: 'Uploaded file',
  github: 'GitHub',
};
export type SkillAction = 'view' | 'edit' | 'copy' | 'remove';

function addedLabel(iso: string): string | null {
  const date = new Date(iso);
  if (!Number.isFinite(date.getTime())) return null;
  const today = new Date();
  const day = (value: Date) => Date.UTC(value.getFullYear(), value.getMonth(), value.getDate());
  const days = Math.max(0, Math.round((day(today) - day(date)) / 86_400_000));
  return days === 0 ? 'Added today' : days === 1 ? 'Added yesterday' : `Added ${days} days ago`;
}

/** Keep library content flexible and its controls fixed, even with long imported descriptions. */
export function SkillRow({
  skill,
  disabled,
  onToggle,
  onAction,
}: {
  skill: SkillSummaryApi;
  disabled: boolean;
  onToggle: () => void;
  onAction: (action: SkillAction) => void;
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();
  const added = addedLabel(skill.created_at);

  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    const outside = (event: PointerEvent) => {
      if (
        !menu.current?.contains(event.target as Node) &&
        !trigger.current?.contains(event.target as Node)
      )
        setOpen(false);
    };
    document.addEventListener('pointerdown', outside);
    return () => document.removeEventListener('pointerdown', outside);
  }, [open]);

  function close() {
    setOpen(false);
    trigger.current?.focus();
  }

  const actions: { action: SkillAction; label: string }[] = [
    { action: 'view', label: 'View details' },
    ...(skill.source === 'github' ? [] : [{ action: 'edit' as const, label: 'Edit skill' }]),
    { action: 'copy', label: 'Create a copy' },
    { action: 'remove', label: 'Remove skill' },
  ];

  return (
    <div className="grid grid-cols-[minmax(0,1fr)_4rem_2rem] items-center gap-2 py-4 sm:grid-cols-[minmax(0,1fr)_8rem_4rem_2rem] sm:gap-3">
      <button
        type="button"
        onClick={() => onAction('view')}
        aria-label={`View ${skill.name}`}
        className="group flex min-w-0 items-center gap-3 text-left"
      >
        <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-gray-200 text-gray-500 dark:border-white/10 dark:text-gray-400">
          <SkillIcon />
        </span>
        <span className="min-w-0">
          <span
            className="block truncate text-sm font-medium text-gray-900 group-hover:text-crimson dark:text-gray-100"
            title={skill.name}
          >
            {skill.name}
          </span>
          <span className="mt-0.5 block truncate text-xs text-gray-500 dark:text-gray-400">
            {SOURCE_LABELS[skill.source]} · {skill.description}
          </span>
          {added ? (
            <time
              dateTime={skill.created_at}
              className="mt-1 block text-xs text-gray-400 sm:hidden dark:text-gray-500"
            >
              {added}
            </time>
          ) : null}
        </span>
      </button>
      <span className="hidden text-right text-xs text-gray-400 sm:block dark:text-gray-500">
        {added ? (
          <time dateTime={skill.created_at} title={new Date(skill.created_at).toLocaleString()}>
            {added}
          </time>
        ) : null}
      </span>
      <span className="flex justify-center">
        <SkillSwitch
          label={`Auto-use ${skill.name} in new chats`}
          checked={skill.default_enabled}
          onChange={onToggle}
          disabled={disabled}
        />
      </span>
      <div className="relative">
        <button
          ref={trigger}
          type="button"
          aria-label={`Manage ${skill.name}`}
          aria-haspopup="menu"
          aria-expanded={open}
          aria-controls={open ? id : undefined}
          onClick={() => setOpen(!open)}
          className="flex h-8 w-8 items-center justify-center rounded-lg text-gray-500 hover:bg-gray-100 focus-visible:outline focus-visible:outline-2 dark:text-gray-400 dark:hover:bg-white/5"
        >
          <svg aria-hidden="true" width="18" height="18" viewBox="0 0 24 24" fill="currentColor">
            <circle cx="12" cy="5" r="1.75" />
            <circle cx="12" cy="12" r="1.75" />
            <circle cx="12" cy="19" r="1.75" />
          </svg>
        </button>
        {open ? (
          <div
            id={id}
            ref={menu}
            role="menu"
            aria-label={`Manage ${skill.name}`}
            className="absolute right-0 top-full z-20 mt-1 w-44 rounded-xl border border-gray-200 bg-white p-1.5 shadow-lg dark:border-white/10 dark:bg-gray-800"
            onKeyDown={(event) => {
              if (event.key === 'Escape') {
                event.preventDefault();
                event.stopPropagation();
                close();
              } else if (event.key === 'Tab') {
                close();
              } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
                event.preventDefault();
                const items = [
                  ...menu.current!.querySelectorAll<HTMLButtonElement>('[role="menuitem"]'),
                ];
                const current = items.indexOf(document.activeElement as HTMLButtonElement);
                const next =
                  event.key === 'Home'
                    ? 0
                    : event.key === 'End'
                      ? items.length - 1
                      : (current + (event.key === 'ArrowDown' ? 1 : -1) + items.length) %
                        items.length;
                items[next]?.focus();
              }
            }}
          >
            {actions.map(({ action, label }) => (
              <button
                key={action}
                type="button"
                role="menuitem"
                tabIndex={-1}
                onClick={() => {
                  close();
                  onAction(action);
                }}
                className={`block w-full rounded-md px-3 py-2 text-left text-sm hover:bg-gray-100 focus:bg-gray-100 focus:outline-none dark:hover:bg-white/10 dark:focus:bg-white/10 ${action === 'remove' ? 'text-red-600 dark:text-red-400' : 'text-gray-700 dark:text-gray-200'}`}
              >
                {label}
              </button>
            ))}
          </div>
        ) : null}
      </div>
    </div>
  );
}
