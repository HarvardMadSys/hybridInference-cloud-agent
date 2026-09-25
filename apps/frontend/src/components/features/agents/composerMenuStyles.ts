/** Shared presentation for the add menu and slash-command suggestions. */
export const COMPOSER_MENU_SURFACE =
  'absolute bottom-[calc(100%+8px)] left-0 right-0 z-50 max-h-[min(28rem,60dvh)] overflow-y-auto rounded-xl border border-gray-200 bg-white p-2 shadow-lg dark:border-white/10 dark:bg-gray-800';

export const COMPOSER_MENU_HEADING =
  'px-3 pb-1 text-xs font-medium text-gray-500 dark:text-gray-400';

export const COMPOSER_MENU_ROW =
  'flex min-h-9 w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm font-normal text-gray-900 transition-colors hover:bg-gray-100 focus:bg-gray-100 focus:outline-none dark:text-gray-100 dark:hover:bg-white/10 dark:focus:bg-white/10 [@media(pointer:coarse)]:min-h-11';

export const COMPOSER_MENU_DETAIL =
  'ml-auto min-w-0 truncate text-right text-sm font-normal text-gray-500 dark:text-gray-400';
