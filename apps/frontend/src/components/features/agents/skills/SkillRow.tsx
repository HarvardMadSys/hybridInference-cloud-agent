'use client';

import { useId, useRef, useState } from 'react';
import type { SkillSummaryApi } from '@/lib/api/skills';
import {
  MENU_POSITIONED,
  MENU_ROW,
  MENU_ROW_DESTRUCTIVE,
  MENU_ACTIONS,
} from '@/components/ui/menuStyles';
import { usePopupMenu } from '@/components/ui/usePopupMenu';
import { SkillIcon, SkillSwitch } from './SkillDialog';

export const SOURCE_LABELS = {
  created: 'Created by you',
  uploaded: 'Uploaded file',
  github: 'GitHub',
};
export type SkillAction = 'view' | 'edit' | 'remove';

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
  const popup = usePopupMenu({
    open,
    onOpenChange: setOpen,
    triggerRef: trigger,
    menuRef: menu,
    placement: { gap: 4, width: 176, maxWidth: 240, preferredHeight: 180, maxHeight: 320 },
  });

  function close() {
    setOpen(false);
    trigger.current?.focus();
  }

  const actions: { action: SkillAction; label: string }[] = [
    { action: 'view', label: 'View details' },
    ...(skill.source === 'github' ? [] : [{ action: 'edit' as const, label: 'Edit skill' }]),
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
          className="ui-focus-ring flex h-8 w-8 items-center justify-center rounded-lg text-muted hover:bg-surface-hover hover:text-foreground"
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
            style={popup.style}
            className={`${MENU_ACTIONS} ${MENU_POSITIONED} w-44`}
            onKeyDown={popup.onKeyDown}
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
                className={action === 'remove' ? MENU_ROW_DESTRUCTIVE : MENU_ROW}
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
