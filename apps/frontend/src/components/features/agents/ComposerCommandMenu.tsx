'use client';

import { useT } from '@/components/providers';
import type { ComposerCommand } from './composerCommands';

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
  if (entries.length === 0) return null;
  return (
    <div className="absolute bottom-[calc(100%+6px)] left-0 right-0 z-50 rounded-xl border border-gray-200 bg-white p-1.5 shadow-lg dark:border-white/10 dark:bg-gray-900">
      <p className="px-2.5 pb-1 pt-1 text-[11px] font-medium text-gray-400 dark:text-gray-500">
        {t('commands.title')}
      </p>
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
            className={`flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13.5px] font-medium transition-colors ${
              index === activeIndex
                ? 'bg-gray-100 text-gray-900 dark:bg-white/10 dark:text-gray-100'
                : 'text-gray-700 hover:bg-gray-50 dark:text-gray-200 dark:hover:bg-white/5'
            }`}
          >
            {entry.icon}
            <span className="shrink-0">{t(entry.labelKey)}</span>
            <span className="ml-auto min-w-0 truncate text-xs font-normal text-gray-500 dark:text-gray-400">
              {t(entry.detailKey)}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
