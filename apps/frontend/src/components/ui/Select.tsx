'use client';

import {
  forwardRef,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ForwardedRef,
  type KeyboardEvent,
  type ReactElement,
  type ReactNode,
  type Ref,
  type RefObject,
} from 'react';
import { createPortal } from 'react-dom';
import { useT } from '@/components/providers/LocaleProvider';
import { MODAL_POPUP } from './useModalDialog';
import { keepInView, useSelectPlacement } from './useSelectPlacement';

export interface SelectOption<T extends string = string> {
  value: T;
  label: string;
  disabled?: boolean;
}

export interface SelectGroup<T extends string = string> {
  label: string;
  options: readonly SelectOption<T>[];
}

export type SelectItem<T extends string = string> = SelectOption<T> | SelectGroup<T>;

export interface SelectProps<T extends string> {
  value: T;
  onValueChange: (value: T) => void;
  options: readonly SelectItem<T>[];
  id?: string;
  name?: string;
  disabled?: boolean;
  /** Blocks form submission while the value is empty, as on a native select. */
  required?: boolean;
  /** Shown when the value matches no option. */
  placeholder?: string;
  /** Filter long menus by typing. Defaults to menus of more than ten options. */
  searchable?: boolean;
  /** Trigger classes. Defaults to the shared `ui-input` field. */
  className?: string;
  /** A decorative icon before the value. */
  icon?: ReactNode;
  title?: string;
  'aria-label'?: string;
  'aria-labelledby'?: string;
  'aria-describedby'?: string;
  'aria-invalid'?: boolean;
}

interface Entry<T extends string> extends SelectOption<T> {
  index: number;
  group?: string;
}

const SEARCH_THRESHOLD = 10;
const PAGE = 10;
const TYPEAHEAD_MS = 500;
/** Keys that open a closed menu, and the option each opens on. */
const OPENING_KEYS: Partial<Record<string, 'value' | 'first' | 'last'>> = {
  ArrowDown: 'value',
  ArrowUp: 'value',
  Home: 'first',
  End: 'last',
};

function flatten<T extends string>(items: readonly SelectItem<T>[]): Entry<T>[] {
  const entries: Entry<T>[] = [];
  for (const item of items) {
    if ('options' in item) {
      for (const option of item.options)
        entries.push({ ...option, index: entries.length, group: item.label });
    } else entries.push({ ...item, index: entries.length });
  }
  return entries;
}

/** Consecutive entries of one group, rendered under its heading. */
function sections<T extends string>(entries: Entry<T>[]) {
  const result: { group?: string; entries: Entry<T>[] }[] = [];
  for (const entry of entries) {
    const last = result.at(-1);
    if (last && last.group === entry.group) last.entries.push(entry);
    else result.push({ group: entry.group, entries: [entry] });
  }
  return result;
}

/**
 * The option a typed prefix reaches. Repeating one letter steps through the
 * options that start with it, as a native select does.
 */
function typeaheadMatch<T extends string>(
  entries: Entry<T>[],
  from: number,
  text: string,
): Entry<T> | undefined {
  const needle = text.toLocaleLowerCase();
  const repeated = [...needle].every((character) => character === needle[0]);
  const prefix = repeated ? needle[0] : needle;
  const start = repeated ? from + 1 : Math.max(from, 0);
  for (let step = 0; step < entries.length; step += 1) {
    const entry = entries[(start + step) % entries.length];
    if (entry.label.toLocaleLowerCase().startsWith(prefix)) return entry;
  }
  return undefined;
}

function moveTarget(key: string, position: number, count: number): number | null {
  if (key === 'ArrowDown') return Math.min(count - 1, position + 1);
  if (key === 'ArrowUp') return Math.max(0, position < 0 ? count - 1 : position - 1);
  if (key === 'Home') return 0;
  if (key === 'End') return count - 1;
  if (key === 'PageDown') return Math.min(count - 1, Math.max(position, 0) + PAGE);
  if (key === 'PageUp') return Math.max(0, position - PAGE);
  return null;
}

function printable(event: KeyboardEvent): boolean {
  return event.key.length === 1 && !event.metaKey && !event.ctrlKey && !event.altKey;
}

function Chevron({ open }: { open: boolean }) {
  return (
    <svg
      aria-hidden
      viewBox="0 0 20 20"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      className={`h-[1.15em] w-[1.15em] shrink-0 text-muted transition-transform ${open ? 'rotate-180' : ''}`}
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="m6 8 4 4 4-4" />
    </svg>
  );
}

export function Check() {
  return (
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
  );
}

/** The panel of a portalled menu, positioned by `useSelectPlacement`. */
export const POPUP_CLASS =
  'ui-popover fixed z-[100] m-0 flex w-max flex-col overflow-hidden rounded-lg border border-border bg-surface-raised text-foreground shadow-card';

export function optionClass(selected: boolean, active: boolean, disabled: boolean) {
  const tone = disabled
    ? 'cursor-not-allowed text-muted opacity-60'
    : selected
      ? 'cursor-pointer bg-accent-subtle font-medium text-accent'
      : 'cursor-pointer text-foreground';
  const highlight = active
    ? selected
      ? 'ring-1 ring-inset ring-accent/30'
      : 'bg-surface-hover'
    : '';
  return `flex min-h-8 items-center gap-2 rounded-md px-2.5 py-1.5 text-[13px] leading-5 [@media(pointer:coarse)]:min-h-11 ${tone} ${highlight}`;
}

/** The trigger's content: the chosen label, sized like a native select to its longest option. */
function TriggerValue({
  icon,
  shown,
  empty,
  labels,
  open,
}: {
  icon?: ReactNode;
  shown: string;
  empty: boolean;
  labels: string[];
  open: boolean;
}) {
  return (
    <span className="flex w-full min-w-0 items-center gap-2">
      {icon ? (
        <span aria-hidden className="flex shrink-0 items-center text-muted">
          {icon}
        </span>
      ) : null}
      <span className="grid min-w-0 flex-1">
        {labels.map((label, index) => (
          <span
            key={index}
            aria-hidden
            data-label={label}
            className="invisible col-start-1 row-start-1 h-0 overflow-hidden whitespace-nowrap after:content-[attr(data-label)]"
          />
        ))}
        <span className={`col-start-1 row-start-1 truncate ${empty ? 'text-muted' : ''}`}>
          {shown || ' '}
        </span>
      </span>
      <Chevron open={open} />
    </span>
  );
}

function SearchField({
  inputRef,
  listId,
  activeId,
  query,
  onQuery,
}: {
  inputRef: RefObject<HTMLInputElement>;
  listId: string;
  activeId?: string;
  query: string;
  onQuery: (query: string) => void;
}) {
  const t = useT();
  return (
    <div className="m-1.5 mb-1 flex shrink-0 items-center gap-2 rounded-md border border-border bg-surface-muted px-2.5 focus-within:border-accent/50">
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
        ref={inputRef}
        role="combobox"
        aria-label={t('picker.search')}
        aria-expanded="true"
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={activeId}
        autoComplete="off"
        spellCheck={false}
        placeholder={t('picker.search')}
        value={query}
        onChange={(event) => onQuery(event.target.value)}
        className="h-8 min-w-0 flex-1 bg-transparent text-base text-foreground outline-none placeholder:text-muted sm:text-[13px]"
      />
    </div>
  );
}

interface MenuProps<T extends string> {
  ids: { base: string; trigger: string; list: string };
  refs: {
    popup: RefObject<HTMLDivElement>;
    list: RefObject<HTMLDivElement>;
    search: RefObject<HTMLInputElement>;
  };
  searchable: boolean;
  query: string;
  onQuery: (query: string) => void;
  matches: Entry<T>[];
  current?: Entry<T>;
  value: T;
  listName: { label?: string; labelledBy?: string };
  onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  onActive: (value: T) => void;
  onChoose: (entry: Entry<T>) => void;
}

function SelectMenu<T extends string>({
  ids,
  refs,
  searchable,
  query,
  onQuery,
  matches,
  current,
  value,
  listName,
  onKeyDown,
  onActive,
  onChoose,
}: MenuProps<T>) {
  const t = useT();
  const activeId = current ? `${ids.base}option-${current.index}` : undefined;
  return (
    <div
      ref={refs.popup}
      {...{ [MODAL_POPUP]: ids.trigger }}
      className={POPUP_CLASS}
      onKeyDown={onKeyDown}
      onPointerDown={(event) => {
        // Keep focus in the menu while pointing at it; only the search box takes focus.
        if (event.target !== refs.search.current) event.preventDefault();
      }}
      onClick={(event) => event.stopPropagation()}
    >
      {searchable ? (
        <SearchField
          inputRef={refs.search}
          listId={ids.list}
          activeId={activeId}
          query={query}
          onQuery={onQuery}
        />
      ) : null}
      <div
        ref={refs.list}
        id={ids.list}
        role="listbox"
        aria-label={listName.label}
        aria-labelledby={listName.label ? undefined : listName.labelledBy}
        tabIndex={searchable ? undefined : 0}
        aria-activedescendant={searchable ? undefined : activeId}
        className="min-h-0 overflow-y-auto overscroll-contain p-1 outline-none"
      >
        {sections(matches).map((section, sectionIndex) => {
          const rows = section.entries.map((entry) => (
            <div
              key={entry.index}
              id={`${ids.base}option-${entry.index}`}
              role="option"
              aria-selected={entry.value === value}
              aria-disabled={entry.disabled || undefined}
              onPointerMove={() => {
                if (!entry.disabled && entry !== current) onActive(entry.value);
              }}
              onClick={() => onChoose(entry)}
              className={optionClass(entry.value === value, entry === current, !!entry.disabled)}
            >
              <span className="min-w-0 flex-1 break-words">{entry.label}</span>
              {entry.value === value ? <Check /> : null}
            </div>
          ));
          if (section.group === undefined) return rows;
          const headingId = `${ids.base}group-${sectionIndex}`;
          return (
            <div key={`group-${sectionIndex}`} role="group" aria-labelledby={headingId}>
              <div
                id={headingId}
                role="presentation"
                className="px-2.5 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-muted"
              >
                {section.group}
              </div>
              {rows}
            </div>
          );
        })}
        {!matches.length ? (
          <p role="status" className="px-2.5 py-5 text-center text-[13px] text-muted">
            {t('picker.noResults')}
          </p>
        ) : null}
      </div>
    </div>
  );
}

/**
 * Focus the open menu, keep its active option in view, and close it when
 * focus or a pointer goes elsewhere.
 */
function useMenuBehaviour(
  visible: boolean,
  refs: {
    trigger: RefObject<HTMLButtonElement>;
    popup: RefObject<HTMLDivElement>;
    list: RefObject<HTMLDivElement>;
    search: RefObject<HTMLInputElement>;
  },
  activeId: string | undefined,
  dismiss: () => void,
) {
  const { trigger, popup, list, search } = refs;
  const onDismiss = useRef(dismiss);
  onDismiss.current = dismiss;

  useLayoutEffect(() => {
    if (visible) (search.current ?? list.current)?.focus({ preventScroll: true });
  }, [visible, search, list]);

  useLayoutEffect(() => {
    if (visible && activeId) keepInView(list.current, document.getElementById(activeId));
  }, [visible, activeId, list]);

  useEffect(() => {
    if (!visible) return;
    function inside(target: Node) {
      return Boolean(popup.current?.contains(target) || trigger.current?.contains(target));
    }
    function onPointerDown(event: Event) {
      const target = event.target as Node;
      // A label of this control forwards its click to the trigger, which toggles the menu.
      const label = target instanceof Element ? target.closest('label') : null;
      const ownLabel = label && [...(trigger.current?.labels ?? [])].includes(label);
      if (!inside(target) && !ownLabel) onDismiss.current();
    }
    function onFocusIn(event: Event) {
      if (!inside(event.target as Node)) onDismiss.current();
    }
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('focusin', onFocusIn);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('focusin', onFocusIn);
    };
  }, [visible, popup, trigger]);
}

/** What the visible menu offers for a query, and which option is active. */
function menuView<T extends string>(
  entries: Entry<T>[],
  searchable: boolean,
  query: string,
  active: string | null,
) {
  const needle = searchable ? query.trim().toLocaleLowerCase() : '';
  const matches = needle
    ? entries.filter((entry) => entry.label.toLocaleLowerCase().includes(needle))
    : entries;
  const enabled = matches.filter((entry) => !entry.disabled);
  const current = enabled.find((entry) => entry.value === active) ?? enabled[0];
  return { matches, enabled, current, position: current ? enabled.indexOf(current) : -1 };
}

/** The option a menu opens on: the chosen one, else the first or last it can choose. */
function startValue<T extends string>(
  entries: Entry<T>[],
  value: T,
  initial: 'first' | 'last' | 'value',
): T | null {
  const choices = entries.filter((entry) => !entry.disabled);
  if (initial === 'last') return choices.at(-1)?.value ?? null;
  const selected = choices.find((entry) => entry.value === value);
  return (initial === 'value' && selected ? selected : choices[0])?.value ?? null;
}

/** The accessible name of a menu: its control's own label. */
export function menuName(
  node: HTMLButtonElement | HTMLInputElement,
  ariaLabel?: string,
): string | undefined {
  const labels = [...(node.labels ?? [])].map((label) => label.textContent?.trim() ?? '');
  return ariaLabel ?? (labels.join(' ') || undefined);
}

export function assignRef<E>(ref: ForwardedRef<E>, node: E | null) {
  if (typeof ref === 'function') ref(node);
  else if (ref) ref.current = node;
}

/**
 * Preserve named form values and native required validation. The browser
 * reports a missing selection here, and focus goes on to the trigger.
 */
function ValidationProxy({
  value,
  name,
  required,
  disabled,
  onFocus,
}: {
  value: string | undefined;
  name?: string;
  required: boolean;
  disabled: boolean;
  onFocus: () => void;
}) {
  if (!required && !name) return null;
  return (
    <input
      type={required ? 'text' : 'hidden'}
      name={name}
      tabIndex={-1}
      aria-hidden
      required={required}
      disabled={disabled}
      value={value ?? ''}
      onChange={() => {}}
      onFocus={onFocus}
      className="pointer-events-none absolute h-px w-px opacity-0"
    />
  );
}

function SelectImpl<T extends string>(
  props: SelectProps<T>,
  forwarded: ForwardedRef<HTMLButtonElement>,
) {
  const { value, onValueChange, options, id, disabled = false, required = false } = props;
  const generated = useId();
  const ids = { base: generated, trigger: id ?? `${generated}trigger`, list: `${generated}list` };
  const trigger = useRef<HTMLButtonElement | null>(null);
  const popup = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);
  const typed = useRef({ text: '', at: 0 });
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState<string | null>(null);
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const [listLabel, setListLabel] = useState<string>();

  const entries = flatten(options);
  const selected = entries.find((entry) => entry.value === value);
  const searchable = props.searchable ?? entries.length > SEARCH_THRESHOLD;
  const view = menuView(entries, searchable, query, active);
  const activeId = view.current ? `${generated}option-${view.current.index}` : undefined;
  const visible = open && !disabled && container !== null;

  useSelectPlacement(visible, trigger, popup, view.matches.length);
  useMenuBehaviour(visible, { trigger, popup, list, search }, activeId, () => setOpen(false));

  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  function show(initial: 'first' | 'last' | 'value' = 'value', seed = '') {
    const node = trigger.current;
    if (!node || disabled) return;
    setListLabel(menuName(node, props['aria-label']));
    // Inside a modal dialog the menu stays in its panel, so the dialog's
    // isolation and focus containment treat it as the dialog's own.
    setContainer(node.closest<HTMLElement>('[aria-modal="true"]') ?? document.body);
    setQuery(seed);
    setActive(startValue(entries, value, initial));
    typed.current = { text: '', at: 0 };
    setOpen(true);
  }

  function close(restoreFocus: boolean) {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus({ preventScroll: true });
  }

  function choose(entry: Entry<T>) {
    if (entry.disabled) return;
    if (entry.value !== value) onValueChange(entry.value);
    close(true);
  }

  function typeahead(key: string, from: Entry<T>[], position: number) {
    const now = Date.now();
    const text = (now - typed.current.at < TYPEAHEAD_MS ? typed.current.text : '') + key;
    typed.current = { text, at: now };
    const match = typeaheadMatch(from, position, text);
    if (match) setActive(match.value);
  }

  function onTriggerKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (visible || event.nativeEvent.isComposing) return;
    const initial = OPENING_KEYS[event.key];
    if (initial) {
      event.preventDefault();
      show(initial);
    } else if (printable(event) && event.key !== ' ') {
      event.preventDefault();
      // A long menu starts searching with the letter; a short one jumps to it.
      show('value', searchable ? event.key : '');
      const choices = entries.filter((entry) => !entry.disabled);
      if (!searchable) typeahead(event.key, choices, -1);
    }
  }

  /** Escape, Tab and choosing; returns whether the key was one of them. */
  function command(event: KeyboardEvent<HTMLDivElement>): boolean {
    const typing = Date.now() - typed.current.at < TYPEAHEAD_MS && typed.current.text !== '';
    if (event.key === 'Tab') {
      // The menu is portalled. Resume the tab order from the trigger; inside a
      // modal dialog, the dialog moves focus on from the trigger itself.
      close(true);
      return true;
    }
    const dismiss = event.key === 'Escape' || (event.key === 'ArrowUp' && event.altKey);
    const commit = event.key === 'Enter' || (event.key === ' ' && !searchable && !typing);
    if (!dismiss && !commit) return false;
    event.preventDefault();
    event.stopPropagation();
    if (dismiss) close(true);
    else if (view.current) choose(view.current);
    return true;
  }

  function onPopupKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || command(event))
      return;
    const target = moveTarget(event.key, view.position, view.enabled.length);
    if (target !== null) {
      event.preventDefault();
      event.stopPropagation();
      const entry = view.enabled[target];
      if (entry) setActive(entry.value);
    } else if (!searchable && printable(event)) {
      event.preventDefault();
      typeahead(event.key, view.enabled, view.position);
    }
  }

  let menu: ReactNode = null;
  if (visible && container)
    menu = createPortal(
      <SelectMenu
        ids={ids}
        refs={{ popup, list, search }}
        searchable={searchable}
        query={query}
        onQuery={(next) => {
          setQuery(next);
          setActive(null);
        }}
        matches={view.matches}
        current={view.current}
        value={value}
        listName={{ label: listLabel, labelledBy: props['aria-labelledby'] }}
        onKeyDown={onPopupKeyDown}
        onActive={setActive}
        onChoose={choose}
      />,
      container,
    );

  return (
    <>
      <button
        ref={(node) => {
          trigger.current = node;
          assignRef(forwarded, node);
        }}
        type="button"
        id={ids.trigger}
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={visible}
        aria-controls={visible ? ids.list : undefined}
        aria-label={props['aria-label']}
        aria-labelledby={props['aria-labelledby']}
        aria-describedby={props['aria-describedby']}
        aria-invalid={props['aria-invalid']}
        aria-required={required || undefined}
        disabled={disabled}
        title={props.title}
        onClick={() => (visible ? close(true) : show())}
        onKeyDown={onTriggerKeyDown}
        className={`ui-select ${props.className ?? 'ui-input'}`}
      >
        <TriggerValue
          icon={props.icon}
          shown={selected?.label ?? props.placeholder ?? ''}
          empty={!selected}
          labels={entries.map((entry) => entry.label).concat(props.placeholder ?? [])}
          open={visible}
        />
      </button>
      <ValidationProxy
        value={selected?.value}
        name={props.name}
        required={required}
        disabled={disabled}
        onFocus={() => trigger.current?.focus()}
      />
      {menu}
    </>
  );
}

/**
 * The shared themed select: a trigger with a keyboard-accessible listbox menu
 * that works inside modal dialogs. Values are strings, as on a native select.
 */
export const Select = forwardRef(SelectImpl) as <T extends string>(
  props: SelectProps<T> & { ref?: Ref<HTMLButtonElement> },
) => ReactElement;
