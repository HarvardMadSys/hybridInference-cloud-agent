'use client';

import { useEffect, useId, useLayoutEffect, useRef, useState, type KeyboardEvent } from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers/LocaleProvider';
import { usePickerLayout } from './usePickerLayout';

/** A compact composer control with a themed, keyboard-accessible option list. */
export function Picker({
  label,
  value,
  options: suppliedOptions,
  onChange,
  compact = false,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (next: string) => void;
  compact?: boolean;
}) {
  const t = useT();
  const options = suppliedOptions.filter((option) => typeof option === 'string');
  const id = useId();
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const typeahead = useRef({ text: '', at: 0 });
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(value);
  const searchable = options.length > 7;
  const needle = query.trim().toLocaleLowerCase();
  const matches = options.filter((option) => option.toLocaleLowerCase().includes(needle));
  const current = matches.includes(active) ? active : (matches[0] ?? '');
  const activeId = matches.length ? `${id}-option-${matches.indexOf(current)}` : undefined;
  const visible = open && options.length > 0;
  const style = usePickerLayout(visible, trigger, panel, searchable, options.length);

  useLayoutEffect(() => {
    if (visible) (search.current ?? list.current)?.focus();
  }, [visible]);

  useLayoutEffect(() => {
    if (!visible || !style || !activeId || !list.current) return;
    const option = document.getElementById(activeId);
    if (!option) return;
    // Measure after the viewport height is applied. Scroll only the list, so
    // opening a picker cannot move the page or its horizontal toolbar.
    const bounds = option.getBoundingClientRect();
    const viewport = list.current.getBoundingClientRect();
    if (bounds.top < viewport.top) list.current.scrollTop -= viewport.top - bounds.top;
    else if (bounds.bottom > viewport.bottom)
      list.current.scrollTop += bounds.bottom - viewport.bottom;
  }, [visible, activeId, current, style]);

  useEffect(() => {
    if (!visible) return;
    function dismiss(event: Event) {
      const target = event.target as Node;
      if (!panel.current?.contains(target) && !trigger.current?.contains(target)) setOpen(false);
    }
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('focusin', dismiss);
    return () => {
      document.removeEventListener('pointerdown', dismiss);
      document.removeEventListener('focusin', dismiss);
    };
  }, [visible]);

  function show() {
    setQuery('');
    setActive(value);
    typeahead.current = { text: '', at: 0 };
    setOpen(true);
  }

  function close() {
    setOpen(false);
    trigger.current?.focus();
  }

  function choose(option: string) {
    onChange(option);
    close();
  }

  function onKeyDown(event: KeyboardEvent) {
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close();
    } else if (event.key === 'Tab') {
      // The panel is portalled. Resume tab order from its composer trigger.
      close();
    } else if (event.key === 'Enter' || (event.key === ' ' && !searchable)) {
      event.preventDefault();
      event.stopPropagation();
      if (matches.length) choose(current);
    } else if (
      ['ArrowDown', 'ArrowUp'].includes(event.key) ||
      (!searchable && ['Home', 'End'].includes(event.key))
    ) {
      event.preventDefault();
      const index = matches.indexOf(current);
      let next = index + (event.key === 'ArrowDown' ? 1 : -1);
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = matches.length - 1;
      next = Math.max(0, Math.min(matches.length - 1, next));
      setActive(matches[next] ?? '');
    } else if (
      !searchable &&
      event.key.length === 1 &&
      !event.metaKey &&
      !event.ctrlKey &&
      !event.altKey
    ) {
      event.preventDefault();
      const now = Date.now();
      const text = (now - typeahead.current.at < 500 ? typeahead.current.text : '') + event.key;
      typeahead.current = { text, at: now };
      const match = options.find((option) =>
        option.toLocaleLowerCase().startsWith(text.toLocaleLowerCase()),
      );
      if (match) setActive(match);
    }
  }

  if (!options.length) return null;

  return (
    <>
      <button
        ref={trigger}
        type="button"
        value={value}
        aria-label={label}
        aria-describedby={`${id}-value`}
        aria-haspopup="dialog"
        aria-expanded={visible}
        aria-controls={visible ? id : undefined}
        title={`${label}: ${value}`}
        onClick={() => (visible ? close() : show())}
        onKeyDown={(event) => {
          if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
            event.preventDefault();
            show();
          }
        }}
        className={`ui-focus-ring inline-flex min-h-8 max-w-full shrink-0 items-center gap-1.5 rounded-lg border px-2 py-1 text-xs text-foreground transition-colors hover:bg-surface-hover [@media(pointer:coarse)]:min-h-11 ${visible ? 'border-border bg-surface-hover' : 'border-transparent'}`}
      >
        {!compact ? <span className="shrink-0 text-muted">{label}</span> : null}
        <span id={`${id}-value`} className="min-w-0 max-w-[11rem] truncate font-medium">
          {value}
        </span>
        <svg
          aria-hidden
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          className={`h-3 w-3 shrink-0 text-muted transition-transform ${visible ? 'rotate-180' : ''}`}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="m6 8 4 4 4-4" />
        </svg>
      </button>
      {visible &&
        createPortal(
          <div
            ref={panel}
            id={id}
            role="dialog"
            aria-label={label}
            style={style}
            className="ui-popover fixed z-[100] flex flex-col overflow-hidden rounded-xl border border-border bg-surface-raised shadow-card"
            onKeyDown={onKeyDown}
          >
            <div className="flex shrink-0 items-center justify-between gap-3 px-3.5 pb-2 pt-3">
              <span className="text-xs font-medium text-muted">{label}</span>
              {searchable ? (
                <span className="text-[11px] tabular-nums text-muted" aria-hidden>
                  {options.length}
                </span>
              ) : null}
            </div>
            {searchable ? (
              <div className="mx-2 mb-2 flex shrink-0 items-center gap-2 rounded-lg border border-border bg-surface-muted px-2.5 focus-within:border-accent/40">
                <svg
                  aria-hidden
                  viewBox="0 0 20 20"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth={1.6}
                  className="h-3.5 w-3.5 shrink-0 text-muted"
                >
                  <circle cx="8.5" cy="8.5" r="5.5" />
                  <path strokeLinecap="round" d="m13 13 4 4" />
                </svg>
                <input
                  ref={search}
                  role="combobox"
                  aria-label={t('picker.search')}
                  aria-expanded="true"
                  aria-controls={`${id}-list`}
                  aria-autocomplete="list"
                  aria-activedescendant={activeId}
                  autoComplete="off"
                  spellCheck={false}
                  placeholder={t('picker.search')}
                  value={query}
                  onChange={(event) => {
                    setQuery(event.target.value);
                    setActive('');
                  }}
                  className="h-9 min-w-0 flex-1 bg-transparent text-base text-foreground outline-none placeholder:text-muted sm:text-[13px]"
                />
              </div>
            ) : null}
            <div
              ref={list}
              id={`${id}-list`}
              role="listbox"
              aria-label={label}
              tabIndex={searchable ? undefined : 0}
              aria-activedescendant={searchable ? undefined : activeId}
              className="min-h-0 overflow-y-auto overscroll-contain px-1.5 pb-1.5 outline-none"
            >
              {matches.map((option, index) => (
                <div
                  key={option}
                  id={`${id}-option-${index}`}
                  role="option"
                  aria-selected={option === value}
                  onPointerMove={() => setActive(option)}
                  onPointerDown={(event) => event.preventDefault()}
                  onClick={() => choose(option)}
                  className={`flex min-h-9 cursor-pointer items-center gap-3 rounded-lg px-2.5 py-2 text-[13px] transition-colors [@media(pointer:coarse)]:min-h-11 ${option === value ? 'bg-accent-subtle font-medium text-accent' : 'text-foreground'} ${option === current ? (option === value ? 'ring-1 ring-inset ring-accent/20' : 'bg-surface-hover') : ''}`}
                >
                  <span className="min-w-0 flex-1 break-words">{option}</span>
                  {option === value ? (
                    <svg
                      aria-hidden
                      viewBox="0 0 20 20"
                      fill="none"
                      stroke="currentColor"
                      strokeWidth={1.8}
                      className="h-4 w-4 shrink-0"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" d="m5 10 3 3 7-7" />
                    </svg>
                  ) : null}
                </div>
              ))}
              {!matches.length ? (
                <p role="status" className="px-2.5 py-6 text-center text-[13px] text-muted">
                  {t('picker.noResults')}
                </p>
              ) : null}
            </div>
          </div>,
          document.body,
        )}
    </>
  );
}
