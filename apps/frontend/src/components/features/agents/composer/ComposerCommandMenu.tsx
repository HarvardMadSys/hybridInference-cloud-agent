'use client';

import { useRef } from 'react';
import { useComposerMenuLayout } from './useComposerMenuLayout';
import { useT } from '@/components/providers';
import type { ComposerCommand } from './composerCommands';
import {
  COMPOSER_MENU_SURFACE,
  COMPOSER_MENU_HEADING,
  COMPOSER_MENU_ROW,
  COMPOSER_MENU_DETAIL,
} from './composerMenuStyles';

// The commands the composer offers, drawn above the box while one is being
// chosen. The reference is the plus menu's own sheet, because this is the same
// kind of surface — a short list hanging off the composer — and a second visual
// language for it would read as a second kind of thing.
//
// The list never takes focus. The box keeps the caret, the menu names the
// highlighted row to it through `aria-activedescendant`, and the pointer is
// stopped from moving focus on the way in: a completion must not end the
// sentence the owner is in the middle of typing, or drop a phone's keyboard.

export function ComposerCommandMenu({
  id,
  entries,
  activeIndex,
  onHighlight,
  onChoose,
}: {
  id: string;
  entries: ComposerCommand[];
  activeIndex: number;
  onHighlight: (index: number) => void;
  onChoose: (index: number) => void;
}) {
  const t = useT();
  const panel = useRef<HTMLDivElement>(null);
  const menuStyle = useComposerMenuLayout(entries.length > 0, panel);
  if (entries.length === 0) return null;
  return (
    <div ref={panel} className={COMPOSER_MENU_SURFACE} style={menuStyle}>
      <p className={COMPOSER_MENU_HEADING}>{t('commands.title')}</p>
      <div role="listbox" id={id} aria-label={t('commands.title')}>
        {entries.map((entry, index) => (
          <button
            key={entry.name}
            id={`${id}-${entry.name}`}
            type="button"
            role="option"
            aria-selected={index === activeIndex}
            // The pointer must not take the caret out of the box: a click here
            // finishes what is being typed, it does not move to a control.
            onMouseDown={(event) => event.preventDefault()}
            onMouseEnter={() => onHighlight(index)}
            onClick={() => onChoose(index)}
            className={`${COMPOSER_MENU_ROW} ${
              index === activeIndex ? 'bg-surface-hover text-foreground' : ''
            }`}
          >
            {entry.icon}
            <span className="shrink-0">{t(entry.labelKey)}</span>
            <span title={t(entry.detailKey)} className={COMPOSER_MENU_DETAIL}>
              {t(entry.detailKey)}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
