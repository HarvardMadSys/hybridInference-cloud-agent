/** Shared presentation for the add menu and slash-command suggestions. */
export const COMPOSER_MENU_SURFACE =
  'ui-popover absolute bottom-[calc(100%+8px)] left-0 right-0 z-50 max-h-[min(28rem,60dvh)] overflow-y-auto rounded-xl border border-border bg-surface-raised p-2 shadow-card';

export const COMPOSER_MENU_HEADING = 'px-2 pb-1.5 pt-1 text-xs font-medium text-muted';

export const COMPOSER_MENU_ROW =
  'flex min-h-9 w-full items-center gap-2 rounded-lg px-2 py-2 text-left text-xs font-normal text-foreground transition-colors hover:bg-surface-hover focus:bg-surface-hover focus:outline-none [@media(pointer:coarse)]:min-h-11';

export const COMPOSER_MENU_DETAIL =
  'ml-auto min-w-0 truncate text-right text-xs font-normal text-muted';
