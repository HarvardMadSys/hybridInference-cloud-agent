'use client';

import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type RefObject,
} from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers/LocaleProvider';
import { popoverContainer, useAnchoredPopover } from '@/components/ui/anchoredPopover';
import { useTopLayerEscape } from '@/components/ui/useTopLayerEscape';
import {
  MENU_EMPTY,
  MENU_LABEL,
  MENU_POSITIONED,
  MENU_SCROLL,
  MENU_SEARCH,
  MENU_SEARCH_INPUT,
  MENU_SURFACE,
  menuOptionClass,
} from '@/components/ui/menuStyles';

/** The menu's shared keyboard contract, kept out of the component's own branching. */
function pickerKeyDown(
  event: KeyboardEvent,
  {
    searchable,
    matches,
    current,
    options,
    optionLabels,
    typeahead,
    setActive,
    choose,
    close,
  }: {
    searchable: boolean;
    matches: string[];
    current: string;
    options: string[];
    optionLabels: Record<string, string>;
    typeahead: { current: { text: string; at: number } };
    setActive: (value: string) => void;
    choose: (value: string) => void;
    close: () => void;
  },
) {
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
    moveActive(event.key, matches, current, setActive);
  } else if (
    !searchable &&
    event.key.length === 1 &&
    !event.metaKey &&
    !event.ctrlKey &&
    !event.altKey
  ) {
    event.preventDefault();
    typeAhead(event.key, options, optionLabels, typeahead, setActive);
  }
}

function moveActive(
  key: string,
  matches: string[],
  current: string,
  setActive: (value: string) => void,
) {
  const index = matches.indexOf(current);
  const step = key === 'ArrowDown' ? 1 : -1;
  let next = key === 'Home' ? 0 : key === 'End' ? matches.length - 1 : index + step;
  next = Math.max(0, Math.min(matches.length - 1, next));
  setActive(matches[next] ?? '');
}

function typeAhead(
  key: string,
  options: string[],
  optionLabels: Record<string, string>,
  typeahead: { current: { text: string; at: number } },
  setActive: (value: string) => void,
) {
  const now = Date.now();
  const text = (now - typeahead.current.at < 500 ? typeahead.current.text : '') + key;
  typeahead.current = { text, at: now };
  const match = options.find((option) =>
    (optionLabels[option] ?? option).toLocaleLowerCase().startsWith(text.toLocaleLowerCase()),
  );
  if (match) setActive(match);
}

/** The menu width follows the control: form fields match themselves, chips stay compact. */
function pickerPlacement(variant: 'compact' | 'field', searchable: boolean) {
  return {
    gap: 6,
    preferredHeight: 240,
    maxHeight: 384,
    width: variant === 'field' ? undefined : searchable ? 320 : 240,
    maxWidth: 384,
  };
}

function pickerClasses(variant: 'compact' | 'field', open: boolean, hasValue: boolean) {
  const field = variant === 'field';
  const button = field
    ? `min-h-11 w-full gap-3 rounded-xl border bg-surface px-3.5 py-3 text-sm shadow-sm enabled:hover:border-accent/40 enabled:hover:bg-surface-hover ${open ? 'border-accent/50' : 'border-border'}`
    : `min-h-8 shrink-0 gap-1.5 rounded-lg border px-2 py-1 text-xs hover:bg-surface-hover [@media(pointer:coarse)]:min-h-11 ${open ? 'border-border bg-surface-hover' : 'border-transparent'}`;
  const empty = field ? 'text-muted' : 'italic text-muted';
  return {
    button: `ui-focus-ring inline-flex max-w-full items-center text-foreground transition-colors disabled:cursor-not-allowed disabled:opacity-60 ${button}`,
    value: `min-w-0 truncate text-left font-medium ${field ? 'flex-1' : 'max-w-[11rem]'} ${hasValue ? '' : empty}`,
    chevron: `shrink-0 text-muted transition-transform ${field ? 'h-4 w-4' : 'h-3 w-3'} ${open ? 'rotate-180' : ''}`,
  };
}

/** Keep the active option in the list, never moving the page or toolbar. */
function useRevealActive(
  visible: boolean,
  activeId: string | undefined,
  current: string,
  style: unknown,
  list: RefObject<HTMLDivElement>,
) {
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
  }, [visible, activeId, current, style, list]);
}

/** Dismiss on a press or focus anywhere outside the trigger and its panel. */
function useDismissOutside(
  visible: boolean,
  panel: RefObject<HTMLDivElement>,
  trigger: RefObject<HTMLButtonElement | null>,
  setOpen: (open: boolean) => void,
) {
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
  }, [visible, panel, trigger, setOpen]);
}

/** A compact composer control with a themed, keyboard-accessible option list. */
export function Picker({
  label,
  value,
  options: suppliedOptions,
  onChange,
  compact = false,
  placeholder,
  triggerRef,
  variant = 'compact',
  disabled = false,
  optionLabels = {},
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (next: string) => void;
  compact?: boolean;
  /**
   * What an empty control reads. Empty is a real state for a setting the owner
   * must choose — the model, which has no safe default — and drawing nothing
   * would look like a rendering fault rather than an answer.
   */
  placeholder?: string;
  /**
   * The trigger button, so a caller can focus the empty control. A callback
   * ref, not a RefObject: this component owns the element and must hand it out
   * without the caller being able to reassign it.
   */
  triggerRef?: (element: HTMLButtonElement | null) => void;
  /** A full-width form field, using the same menu and keyboard navigation. */
  variant?: 'compact' | 'field';
  disabled?: boolean;
  optionLabels?: Record<string, string>;
}) {
  const t = useT();
  const options = suppliedOptions.filter((option) => typeof option === 'string');
  const id = useId();
  const trigger = useRef<HTMLButtonElement | null>(null);
  const panel = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const typeahead = useRef({ text: '', at: 0 });
  const shown = (value && (optionLabels[value] ?? value)) || placeholder || '';
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(value);
  const searchable = options.length > 7;
  const needle = query.trim().toLocaleLowerCase();
  const matches = options.filter((option) =>
    (optionLabels[option] ?? option).toLocaleLowerCase().includes(needle),
  );
  const current = matches.includes(active) ? active : (matches[0] ?? '');
  const activeId = matches.length ? `${id}-option-${matches.indexOf(current)}` : undefined;
  const visible = open && !disabled && options.length > 0;
  const classes = pickerClasses(variant, visible, Boolean(value));
  const style = useAnchoredPopover(visible, trigger, panel, pickerPlacement(variant, searchable));
  const [container, setContainer] = useState<HTMLElement | null>(null);

  const placed = style.visibility !== 'hidden';
  useLayoutEffect(() => {
    // A hidden popup cannot take focus; only focus it once placement has run.
    if (visible && placed) (search.current ?? list.current)?.focus();
  }, [visible, placed]);

  useRevealActive(visible, activeId, current, style, list);
  useDismissOutside(visible, panel, trigger, setOpen);
  useTopLayerEscape(visible, () => close(), trigger);

  function show() {
    setContainer(popoverContainer(trigger.current));
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
    pickerKeyDown(event, {
      searchable,
      matches,
      current,
      options,
      optionLabels,
      typeahead,
      setActive,
      choose,
      close,
    });
  }

  if (!options.length && variant !== 'field') return null;

  return (
    <>
      <button
        ref={(element) => {
          trigger.current = element;
          if (typeof triggerRef === 'function') triggerRef(element);
        }}
        type="button"
        disabled={disabled || !options.length}
        value={value}
        aria-label={label}
        aria-describedby={`${id}-value`}
        aria-haspopup="dialog"
        aria-expanded={visible}
        aria-controls={visible ? id : undefined}
        title={`${label}: ${shown}`}
        onClick={() => (visible ? close() : show())}
        onKeyDown={(event) => {
          if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
            event.preventDefault();
            show();
          }
        }}
        className={classes.button}
      >
        {!compact && variant !== 'field' ? (
          <span className="shrink-0 text-muted">{label}</span>
        ) : null}
        <span id={`${id}-value`} className={classes.value}>
          {shown}
        </span>
        <svg
          aria-hidden
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.6}
          className={classes.chevron}
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
            className={`${MENU_SURFACE} ${MENU_POSITIONED}`}
            onKeyDown={onKeyDown}
          >
            <div className="flex shrink-0 items-center justify-between gap-3 px-3.5 pb-1 pt-3">
              <span className={MENU_LABEL}>{label}</span>
              {searchable ? (
                <span className="text-[11px] tabular-nums text-muted" aria-hidden>
                  {options.length}
                </span>
              ) : null}
            </div>
            {searchable ? (
              <div className={`${MENU_SEARCH} !my-0`}>
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
                  className={`${MENU_SEARCH_INPUT} h-9`}
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
              className={MENU_SCROLL}
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
                  className={menuOptionClass(option === value, option === current, false)}
                >
                  <span className="min-w-0 flex-1 break-words">
                    {optionLabels[option] ?? option}
                  </span>
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
                <p role="status" className={MENU_EMPTY}>
                  {t('picker.noResults')}
                </p>
              ) : null}
            </div>
          </div>,
          container ?? document.body,
        )}
    </>
  );
}
