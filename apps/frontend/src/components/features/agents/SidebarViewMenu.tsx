'use client';

import { useId, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import {
  MENU_HEADING,
  MENU_ROW,
  MENU_ROW_SELECTED,
  MENU_SURFACE,
  MENU_DIVIDER,
} from '@/components/ui/menuStyles';
import { usePopupMenu } from '@/components/ui/usePopupMenu';
import type { SidebarPreferences } from './sidebarPreferences';

export function SidebarViewMenu({
  value,
  onChange,
}: {
  value: SidebarPreferences;
  onChange: (field: 'group' | 'order', value: string) => void;
}) {
  const t = useT();
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const popup = usePopupMenu({
    open,
    onOpenChange: setOpen,
    triggerRef: trigger,
    menuRef: menu,
    placement: { gap: 6, width: 220, maxWidth: 260, preferredHeight: 240, maxHeight: 360 },
    initialFocus: 'selected',
  });
  return (
    <>
      <button
        ref={trigger}
        type="button"
        aria-label={t('sidebar.viewOptions')}
        title={t('sidebar.viewOptions')}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        className="ui-focus-ring rounded-md p-1 text-muted transition-colors hover:bg-surface-hover hover:text-foreground"
        onClick={() => setOpen((current) => !current)}
      >
        <svg
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.8"
          aria-hidden="true"
        >
          <path d="M3 6h7m4 0h7M3 12h12m4 0h2M3 18h3m4 0h11" />
          <circle cx="12" cy="6" r="2" />
          <circle cx="17" cy="12" r="2" />
          <circle cx="8" cy="18" r="2" />
        </svg>
      </button>
      {open ? (
        <div
          ref={menu}
          id={id}
          role="menu"
          aria-label={t('sidebar.viewOptions')}
          style={popup.style}
          className={`${MENU_SURFACE} fixed z-[100]`}
          onKeyDown={popup.onKeyDown}
        >
          {(['group', 'order'] as const).map((field, index) => (
            <div
              role="group"
              aria-label={t(field === 'group' ? 'sidebar.groupBy' : 'sidebar.orderBy')}
              key={field}
            >
              {index > 0 ? <div className={MENU_DIVIDER} role="presentation" /> : null}
              <div className={MENU_HEADING}>
                {t(field === 'group' ? 'sidebar.groupBy' : 'sidebar.orderBy')}
              </div>
              {(field === 'group'
                ? (['project', 'flat'] as const)
                : (['manual', 'updated'] as const)
              ).map((option) => {
                const checked = value[field] === option;
                return (
                  <button
                    key={option}
                    role="menuitemradio"
                    aria-checked={checked}
                    tabIndex={-1}
                    className={checked ? MENU_ROW_SELECTED : MENU_ROW}
                    onClick={() => {
                      onChange(field, option);
                      setOpen(false);
                      trigger.current?.focus();
                    }}
                  >
                    <span className="min-w-0 flex-1 truncate">
                      {t(
                        option === 'project'
                          ? 'sidebar.byProject'
                          : option === 'flat'
                            ? 'sidebar.inOneList'
                            : option === 'manual'
                              ? 'sidebar.manual'
                              : 'sidebar.lastUpdated',
                      )}
                    </span>
                    {checked ? <span aria-hidden="true">✓</span> : null}
                  </button>
                );
              })}
            </div>
          ))}
        </div>
      ) : null}
    </>
  );
}
