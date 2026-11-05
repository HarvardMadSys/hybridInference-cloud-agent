'use client';

import {
  forwardRef,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ForwardedRef,
  type InputHTMLAttributes,
  type KeyboardEvent,
  type CSSProperties,
  type ReactNode,
  type RefObject,
} from 'react';
import { createPortal } from 'react-dom';
import { assignRef, Check, menuName, optionClass, POPUP_CLASS } from './Select';
import { keepInView, useAnchoredPopover } from './anchoredPopover';
import { useTopLayerEscape } from './useTopLayerEscape';
import { MENU_SCROLL } from './menuStyles';
import { MODAL_POPUP } from './useModalDialog';

export interface SuggestionInputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Values offered while typing. The field still takes any text; none offers nothing. */
  suggestions?: readonly string[] | null;
}

/** Enter a value as typing would, so the field's own `onChange` reports it. */
function enter(input: HTMLInputElement, value: string) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set?.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

function matching(suggestions: readonly string[], filter: string): readonly string[] {
  const needle = filter.trim().toLocaleLowerCase();
  return needle
    ? suggestions.filter((suggestion) => suggestion.toLocaleLowerCase().includes(needle))
    : suggestions;
}

/** What makes the input a combobox, once it has suggestions to offer. */
function comboboxRole(combobox: boolean, expanded: boolean) {
  return combobox
    ? ({ role: 'combobox', 'aria-autocomplete': 'list', 'aria-expanded': expanded } as const)
    : {};
}

function SuggestionList({
  ids,
  refs,
  matches,
  position,
  style,
  value,
  name,
  onActive,
  onChoose,
}: {
  ids: { owner?: string; base: string };
  refs: { popup: RefObject<HTMLDivElement>; list: RefObject<HTMLDivElement> };
  matches: readonly string[];
  position: number;
  style: CSSProperties;
  value: unknown;
  name: { label?: string; labelledBy?: string };
  onActive: (suggestion: string) => void;
  onChoose: (suggestion: string) => void;
}) {
  return (
    <div
      ref={refs.popup}
      {...{ [MODAL_POPUP]: ids.owner }}
      style={style}
      className={POPUP_CLASS}
      // Pointing at a suggestion keeps focus, and the caret, in the field.
      onMouseDown={(event) => event.preventDefault()}
      onClick={(event) => event.stopPropagation()}
    >
      <div
        ref={refs.list}
        id={`${ids.base}list`}
        role="listbox"
        aria-label={name.label}
        aria-labelledby={name.label ? undefined : name.labelledBy}
        className={MENU_SCROLL}
      >
        {matches.map((suggestion, index) => (
          <div
            key={suggestion}
            id={`${ids.base}option-${index}`}
            role="option"
            aria-selected={index === position}
            onPointerMove={() => {
              if (index !== position) onActive(suggestion);
            }}
            onClick={() => onChoose(suggestion)}
            className={optionClass(suggestion === value, index === position, false)}
          >
            <span className="min-w-0 flex-1 break-words">{suggestion}</span>
            {suggestion === value ? <Check /> : null}
          </div>
        ))}
      </div>
    </div>
  );
}

function SuggestionInputImpl(
  { suggestions, ...props }: SuggestionInputProps,
  forwarded: ForwardedRef<HTMLInputElement>,
) {
  const generated = useId();
  const offered = suggestions ?? [];
  const combobox = offered.length > 0;
  const editable = combobox && !props.disabled && !props.readOnly;
  const id = props.id ?? (combobox ? `${generated}input` : undefined);
  const input = useRef<HTMLInputElement | null>(null);
  const popup = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const [active, setActive] = useState<string | null>(null);
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const [listLabel, setListLabel] = useState<string>();

  const matches = matching(offered, filter);
  const position = active === null ? -1 : matches.indexOf(active);
  const visible = open && editable && matches.length > 0 && container !== null;
  const activeId = visible && position >= 0 ? `${generated}option-${position}` : undefined;

  const popoverStyle = useAnchoredPopover(visible, input, popup);
  // The caret stays in the field; Escape dismisses the list, not the page.
  useTopLayerEscape(visible, () => setOpen(false), input);
  useLayoutEffect(() => {
    if (activeId) keepInView(list.current, document.getElementById(activeId));
  }, [activeId]);

  function show(text: string, start: string | null) {
    const node = input.current;
    if (!node || !editable) return;
    setListLabel(menuName(node, props['aria-label']));
    // Inside a modal dialog the list stays in its panel, as a Select's menu does.
    setContainer(node.closest<HTMLElement>('[aria-modal="true"]') ?? document.body);
    setFilter(text);
    setActive(start);
    setOpen(true);
  }

  function choose(suggestion: string) {
    if (input.current && input.current.value !== suggestion) enter(input.current, suggestion);
    setOpen(false);
  }

  /** Arrows open the list, or move through it. */
  function step(by: number, current: string) {
    if (!visible) {
      // Opening by arrow offers every suggestion, starting from the current value.
      const edge = by > 0 ? offered[0] : offered[offered.length - 1];
      show('', offered.includes(current) ? current : edge);
      return;
    }
    const from = position < 0 ? (by > 0 ? -1 : matches.length) : position;
    setActive(matches[Math.min(matches.length - 1, Math.max(0, from + by))]);
  }

  /** Handles the key if the suggestions use it. */
  function navigate(event: KeyboardEvent<HTMLInputElement>): boolean {
    const by = event.key === 'ArrowDown' ? 1 : event.key === 'ArrowUp' ? -1 : 0;
    const choosing = event.key === 'Enter' && position >= 0;
    if (by) step(by, event.currentTarget.value);
    else if (visible && (event.key === 'Escape' || choosing)) {
      // The list is this field's own; outer Escape and Enter handlers never see the key.
      event.stopPropagation();
      if (choosing) choose(matches[position]);
      else setOpen(false);
    } else {
      // Tab moves on as usual; Enter without a highlighted suggestion submits.
      if (event.key === 'Tab' || event.key === 'Enter') setOpen(false);
      return false;
    }
    event.preventDefault();
    return true;
  }

  let menu: ReactNode = null;
  if (visible && container)
    menu = createPortal(
      <SuggestionList
        ids={{ owner: id, base: generated }}
        refs={{ popup, list }}
        matches={matches}
        position={position}
        style={popoverStyle}
        value={props.value}
        name={{ label: listLabel, labelledBy: props['aria-labelledby'] }}
        onActive={setActive}
        onChoose={choose}
      />,
      container,
    );

  return (
    <>
      <input
        {...props}
        {...comboboxRole(combobox, visible)}
        ref={(node) => {
          input.current = node;
          assignRef(forwarded, node);
        }}
        id={id}
        aria-controls={visible ? `${generated}list` : undefined}
        aria-activedescendant={activeId}
        onChange={(event) => {
          props.onChange?.(event);
          // Typing filters the list, and nothing is highlighted until an arrow says so.
          show(event.target.value, null);
        }}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing || !combobox || !navigate(event))
            props.onKeyDown?.(event);
        }}
        onClick={(event) => {
          props.onClick?.(event);
          if (!visible) show('', event.currentTarget.value);
        }}
        onBlur={(event) => {
          props.onBlur?.(event);
          setOpen(false);
        }}
      />
      {menu}
    </>
  );
}

/**
 * A text input with a themed suggestion list, in place of a native datalist.
 * Focus stays in the field: typing filters the list, arrows move through it,
 * Enter or a click enters a suggestion, and Escape, Tab or leaving the field
 * closes it without changing the value. Every other prop goes to the input.
 */
export const SuggestionInput = forwardRef(SuggestionInputImpl);
