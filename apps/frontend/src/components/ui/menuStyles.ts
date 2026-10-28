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

/** A panel hanging off the composer, laid out by `useComposerMenuLayout`. */
export const MENU_COMPOSER =
  'absolute bottom-[calc(100%+8px)] left-0 right-0 z-50 max-h-[min(28rem,60dvh)] rounded-xl border border-border bg-surface-raised text-foreground shadow-card';

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

/** A footer action in a menu, such as Refresh or Configure. */
export const MENU_FOOTER_ACTION =
  'flex min-h-9 min-w-0 flex-1 items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] text-foreground transition-colors hover:bg-surface-hover focus-visible:bg-surface-hover focus-visible:outline-none [@media(pointer:coarse)]:min-h-11';

export function menuRowClass({
  selected = false,
  active = false,
  disabled = false,
  destructive = false,
}: {
  selected?: boolean;
  active?: boolean;
  disabled?: boolean;
  destructive?: boolean;
} = {}): string {
  const tone = disabled
    ? 'cursor-not-allowed text-muted opacity-50'
    : destructive
      ? 'cursor-pointer text-status-error hover:bg-status-error/10 focus-visible:bg-status-error/10'
      : selected
        ? 'cursor-pointer bg-accent-subtle font-medium text-accent'
        : 'cursor-pointer text-foreground';
  const highlight =
    !disabled && !destructive && active
      ? selected
        ? 'ring-1 ring-inset ring-accent/30'
        : 'bg-surface-hover'
      : '';
  return `flex min-h-9 w-full items-center gap-2 rounded-lg px-2.5 py-1.5 text-left text-[13px] leading-5 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-inset focus-visible:ring-accent/40 [@media(pointer:coarse)]:min-h-11 ${tone} ${highlight}`;
}

/** An option row for a listbox, where selection and keyboard highlight differ. */
export function menuOptionClass(selected: boolean, active: boolean, disabled: boolean): string {
  return menuRowClass({ selected, active, disabled });
}

/** A destructive item in an action menu. */
export const MENU_ROW_DESTRUCTIVE = menuRowClass({ destructive: true });

/** An ordinary item in an action menu. */
export const MENU_ROW = menuRowClass();

/** A selected (checkmarked) item in a menu. */
export const MENU_ROW_SELECTED = menuRowClass({ selected: true });
