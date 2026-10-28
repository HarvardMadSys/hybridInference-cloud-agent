'use client';

import { useId, useRef, useState } from 'react';
import { MENU_POSITIONED, MENU_ROW, MENU_SURFACE } from '@/components/ui/menuStyles';
import { usePopupMenu } from '@/components/ui/usePopupMenu';

export default function ServerActions({
  items,
}: {
  items: { label: string; onSelect: () => void }[];
}) {
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const id = useId();
  const popup = usePopupMenu({
    open,
    onOpenChange: setOpen,
    triggerRef: trigger,
    menuRef: menu,
    placement: { gap: 6, width: 192, maxWidth: 260, preferredHeight: 120, maxHeight: 320 },
  });
  if (!items.length) return null;
  return (
    <div className="relative">
      <button
        ref={trigger}
        type="button"
        aria-label="Server actions"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        className="ui-icon-button ui-button-ghost h-8 w-8 text-base"
        onClick={() => setOpen(!open)}
      >
        ⋯
      </button>
      {open && (
        <div
          ref={menu}
          id={id}
          role="menu"
          aria-label="Server actions"
          style={popup.style}
          className={`${MENU_SURFACE} ${MENU_POSITIONED} w-48`}
          onKeyDown={popup.onKeyDown}
        >
          {items.map((item) => (
            <button
              key={item.label}
              role="menuitem"
              type="button"
              tabIndex={-1}
              className={MENU_ROW}
              onClick={() => {
                setOpen(false);
                trigger.current?.focus();
                item.onSelect();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
