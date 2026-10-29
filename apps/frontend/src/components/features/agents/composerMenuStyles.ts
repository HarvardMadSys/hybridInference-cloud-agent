import { MENU_COMPOSER, MENU_HEADING, MENU_ROW, menuRowClass } from '@/components/ui/menuStyles';

/** Shared presentation for the add menu and slash-command suggestions. */
export const COMPOSER_MENU_SURFACE = `${MENU_COMPOSER} overflow-y-auto p-2`;

export const COMPOSER_MENU_HEADING = MENU_HEADING;

export const COMPOSER_MENU_ROW = MENU_ROW;

export const COMPOSER_MENU_ROW_SELECTED = menuRowClass({ selected: true });

export const COMPOSER_MENU_DETAIL =
  'ml-auto min-w-0 truncate text-right text-xs font-normal text-muted';
