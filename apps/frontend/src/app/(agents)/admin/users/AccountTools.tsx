'use client';

// The People view's own tools, behind one menu beside the views: the sign-up
// rules (email domains approved automatically, and clearing old sign-in
// records) and email announcements to accounts. Each opens its own page.

import Link from 'next/link';
import { useId, useRef, useState } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { MENU_ACTIONS, MENU_POSITIONED, MENU_ROW_START } from '@/components/ui/menuStyles';
import { usePopupMenu } from '@/components/ui/usePopupMenu';
import type { TranslationKey } from '@/lib/i18n/translate';

const TOOLS: { href: string; label: TranslationKey; hint: TranslationKey }[] = [
  {
    href: '/admin/users/policy',
    label: 'adminAccounts.tool.policy',
    hint: 'adminAccounts.tool.policyHint',
  },
  {
    href: '/admin/users/mail',
    label: 'adminAccounts.tool.mail',
    hint: 'adminAccounts.tool.mailHint',
  },
];

export function AccountTools() {
  const t = useT();
  const id = useId();
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const popup = usePopupMenu({
    open,
    onOpenChange: setOpen,
    triggerRef: trigger,
    menuRef: menu,
    placement: { gap: 4, width: 288, maxWidth: 320, preferredHeight: 160, maxHeight: 320 },
  });
  return (
    <div className="relative">
      <button
        ref={trigger}
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen(!open)}
        onKeyDown={popup.triggerKeyDown}
        className="ui-focus-ring inline-flex min-h-8 items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-[13px] font-medium text-foreground transition-colors hover:bg-surface-hover"
      >
        {t('adminAccounts.tools')}
        <svg
          aria-hidden="true"
          viewBox="0 0 16 16"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          className={`h-3.5 w-3.5 text-muted transition-transform ${open ? 'rotate-180' : ''}`}
        >
          <path d="m4 6 4 4 4-4" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open && (
        <div
          id={id}
          ref={menu}
          role="menu"
          aria-label={t('adminAccounts.tools')}
          style={popup.style}
          className={`${MENU_ACTIONS} ${MENU_POSITIONED} w-72`}
          onKeyDown={popup.onKeyDown}
        >
          {TOOLS.map((tool) => (
            <Link
              key={tool.href}
              href={tool.href}
              role="menuitem"
              tabIndex={-1}
              className={`${MENU_ROW_START} flex-col !gap-0`}
              onClick={() => setOpen(false)}
            >
              <span className="font-medium">{t(tool.label)}</span>
              <span className="text-xs text-muted">{t(tool.hint)}</span>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
