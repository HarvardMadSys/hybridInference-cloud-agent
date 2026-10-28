/**
 * The one visual language for every floating selection list and popup menu.
 *
 * Menus keep their own semantics — a listbox chooses one value, a menu runs
 * an action, a repository list has a search field and a footer — but they are
 * all the same surface: one radius, border, shadow, row density, and set of
 * hover, focus, selected, disabled and destructive states over the semantic
 * theme tokens. Components compose these constants instead of growing another
 * palette.
 */

/** The panel every menu and selection popup draws. */
export const MENU_SURFACE =
  'flex flex-col overflow-hidden rounded-xl border border-border bg-surface-raised text-foreground shadow-card';

/** A panel placed with `useAnchoredPopover`. */
export const MENU_POSITIONED = 'fixed z-[100] m-0';

/**
 * A panel holding only action rows: it scrolls as a whole, because those rows
 * have no header or footer that must stay put. A short viewport must never
 * clip the actions below the fold.
 */
export const MENU_ACTIONS =
  'flex flex-col overflow-y-auto overscroll-contain rounded-xl border border-border bg-surface-raised p-1.5 text-foreground shadow-card';

/** A panel hanging off the composer, laid out by `useComposerMenuLayout`. */
export const MENU_COMPOSER =
  'flex flex-col overflow-hidden absolute bottom-[calc(100%+8px)] left-0 right-0 z-50 max-h-[min(28rem,60dvh)] rounded-xl border border-border bg-surface-raised text-foreground shadow-card';

/** The scrolling body of a menu, sized by its panel. */
export const MENU_SCROLL = 'min-h-0 flex-1 overflow-y-auto overscroll-contain p-1.5 outline-none';

/** A group label inside a menu. */
export const MENU_HEADING =
  'px-2.5 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-muted';

/** A panel label at the top of a menu, such as the picker's name. */
export const MENU_LABEL = 'px-3.5 pb-1 pt-3 text-xs font-medium text-muted';

/** A separator between menu groups. */
export const MENU_DIVIDER = 'my-1 h-px shrink-0 bg-border';

/** A horizontal rule between a menu body and its footer. */
export const MENU_FOOTER =
  'flex shrink-0 flex-wrap items-center gap-1 border-t border-border p-1.5';

/** The framed search row shared by long selection menus. */
export const MENU_SEARCH =
  'm-1.5 mb-1 flex shrink-0 items-center gap-2 rounded-lg border border-border bg-surface-muted px-2.5 focus-within:border-accent/50';

/** The text field inside `MENU_SEARCH`. */
export const MENU_SEARCH_INPUT =
  'h-8 min-w-0 flex-1 bg-transparent text-base text-foreground outline-none placeholder:text-muted sm:text-[13px]';

/** A muted status line for an empty or filtered menu. */
export const MENU_EMPTY = 'px-2.5 py-5 text-center text-[13px] text-muted';

/** An empty/status note aligned with left-aligned explanatory copy. */
export const MENU_EMPTY_START = 'px-2.5 py-5 text-left text-[13px] text-muted';

/** A footer action in a menu, such as Refresh or Configure. */
export const MENU_FOOTER_ACTION =
  'flex min-h-9 min-w-0 flex-1 items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] text-foreground transition-colors hover:bg-surface-hover focus-visible:bg-surface-hover focus-visible:outline-none [@media(pointer:coarse)]:min-h-11';

/** An accented footer action, such as a link to manage what the menu lists. */
export const MENU_FOOTER_ACTION_ACCENT = `${MENU_FOOTER_ACTION} text-accent`;

/**
 * The states every menu row shares. `hover:`/`focus:` work for buttons,
 * anchors, links and role="menuitem" elements alike; `:enabled` would only
 * match form controls and would leave link rows unhighlighted. Disabled
 * controls and rows that announce `aria-disabled` opt back out of both.
 */
const SUPPRESS_HIGHLIGHT =
  'disabled:hover:bg-transparent disabled:focus:bg-transparent aria-disabled:hover:bg-transparent aria-disabled:focus:bg-transparent disabled:cursor-not-allowed disabled:opacity-50 aria-disabled:cursor-not-allowed aria-disabled:opacity-50';

export function menuRowClass({
  selected = false,
  active = false,
  disabled = false,
  destructive = false,
  accent = false,
  align = 'center',
}: {
  selected?: boolean;
  active?: boolean;
  disabled?: boolean;
  destructive?: boolean;
  /** An ordinary row whose action carries emphasis, such as creating a repo. */
  accent?: boolean;
  /** `start` for a two-line row whose checkmark belongs beside the title. */
  align?: 'center' | 'start';
} = {}): string {
  // Literal utilities, so Tailwind's scanner emits both variants.
  const base = `flex min-h-9 w-full gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] leading-5 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/40 [@media(pointer:coarse)]:min-h-11 ${
    align === 'start' ? 'items-start' : 'items-center'
  }`;
  if (disabled) return `${base} cursor-not-allowed text-muted opacity-50`;
  // Each state names its own hover/focus background, so the selected and
  // destructive treatments never fall back to the ordinary row tint.
  if (destructive)
    return `${base} cursor-pointer text-status-error hover:bg-status-error/10 focus:bg-status-error/10 ${SUPPRESS_HIGHLIGHT}`;
  if (selected)
    return `${base} cursor-pointer bg-accent-subtle font-medium text-accent hover:bg-accent-subtle focus:bg-accent-subtle ${
      active ? 'ring-1 ring-inset ring-accent/30' : ''
    } ${SUPPRESS_HIGHLIGHT}`;
  const tone = accent ? 'text-accent' : 'text-foreground';
  return `${base} cursor-pointer ${tone} hover:bg-surface-hover focus:bg-surface-hover ${
    active ? 'bg-surface-hover' : ''
  } ${SUPPRESS_HIGHLIGHT}`;
}

/** An option row for a listbox, where selection and keyboard highlight differ. */
export function menuOptionClass(selected: boolean, active: boolean, disabled: boolean): string {
  return menuRowClass({ selected, active, disabled });
}

/** A destructive item in an action menu. */
export const MENU_ROW_DESTRUCTIVE = menuRowClass({ destructive: true });

/** An ordinary item in an action menu. */
export const MENU_ROW = menuRowClass();

/**
 * A non-interactive row inside a menu, such as an MCP server with a switch.
 * It keeps the shared density and tokens but must not highlight like an action.
 */
export const MENU_ROW_STATIC =
  'flex min-h-9 w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] leading-5 text-foreground [@media(pointer:coarse)]:min-h-11';

/** A selected (checkmarked) item in a menu. */
export const MENU_ROW_SELECTED = menuRowClass({ selected: true });

/** A two-line ordinary row whose checkmark belongs beside its title. */
export const MENU_ROW_START = menuRowClass({ align: 'start' });

/** A two-line selected row whose checkmark belongs beside its title. */
export const MENU_ROW_SELECTED_START = menuRowClass({ selected: true, align: 'start' });
