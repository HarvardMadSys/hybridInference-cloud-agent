export const SHORTCUT_ACTIONS = ['search', 'sidebar', 'workspace'] as const;
export type ShortcutAction = (typeof SHORTCUT_ACTIONS)[number];

/** The primary modifier is Command on macOS or Control on Windows/Linux. */
export interface ShortcutBinding {
  key: string;
  shift: boolean;
}
export type KeyboardShortcuts = Readonly<Record<ShortcutAction, ShortcutBinding | null>>;

export const DEFAULT_KEYBOARD_SHORTCUTS: KeyboardShortcuts = Object.freeze({
  search: Object.freeze({ key: 'k', shift: false }),
  sidebar: Object.freeze({ key: 'b', shift: false }),
  workspace: Object.freeze({ key: 'j', shift: false }),
});

export const SHORTCUT_LABELS = {
  search: 'settings.shortcuts.search',
  sidebar: 'settings.shortcuts.sidebar',
  workspace: 'settings.shortcuts.workspace',
} as const;

// Keep common editing, navigation and browser-window commands available.
// Chords outside this set may still be claimed by an OS or browser extension.
const RESERVED_KEYS = new Set('acdfhlnopqrstuvwxyz');
const RESERVED_SHIFT_KEYS = new Set('cijnpqrtwz');

export function isReservedShortcut(binding: ShortcutBinding): boolean {
  return (binding.shift ? RESERVED_SHIFT_KEYS : RESERVED_KEYS).has(binding.key);
}

export function isShortcutBinding(value: unknown): value is ShortcutBinding {
  if (!value || typeof value !== 'object') return false;
  const binding = value as Partial<ShortcutBinding>;
  return (
    typeof binding.key === 'string' &&
    /^[a-z]$/.test(binding.key) &&
    typeof binding.shift === 'boolean' &&
    !isReservedShortcut(binding as ShortcutBinding)
  );
}

export function shortcutConflict(
  shortcuts: KeyboardShortcuts,
  action: ShortcutAction,
  binding: ShortcutBinding | null,
): ShortcutAction | undefined {
  if (!binding) return undefined;
  return SHORTCUT_ACTIONS.find(
    (other) =>
      other !== action &&
      shortcuts[other]?.key === binding.key &&
      shortcuts[other]?.shift === binding.shift,
  );
}

export function parseKeyboardShortcuts(value: string | null): KeyboardShortcuts {
  try {
    const record: unknown = JSON.parse(value ?? 'null');
    if (!record || typeof record !== 'object' || Array.isArray(record))
      return DEFAULT_KEYBOARD_SHORTCUTS;
    const stored = record as Record<string, unknown>;
    const next = { ...DEFAULT_KEYBOARD_SHORTCUTS };
    for (const action of SHORTCUT_ACTIONS) {
      if (stored[action] === null) next[action] = null;
      else if (isShortcutBinding(stored[action])) next[action] = stored[action];
    }
    // A malformed or older record must never bind one chord to two actions.
    if (SHORTCUT_ACTIONS.some((action) => shortcutConflict(next, action, next[action])))
      return DEFAULT_KEYBOARD_SHORTCUTS;
    return next;
  } catch {
    return DEFAULT_KEYBOARD_SHORTCUTS;
  }
}

export function shortcutLabel(binding: ShortcutBinding): string {
  return `⌘ / Ctrl + ${binding.shift ? 'Shift + ' : ''}${binding.key.toUpperCase()}`;
}

export function shortcutAriaLabel(binding: ShortcutBinding | null): string | undefined {
  if (!binding) return undefined;
  const keys = `${binding.shift ? 'Shift+' : ''}${binding.key.toUpperCase()}`;
  return `Meta+${keys} Control+${keys}`;
}

/** All app handlers opt out while Settings is recording a replacement chord. */
export function matchesShortcut(event: KeyboardEvent, binding: ShortcutBinding | null): boolean {
  return Boolean(
    binding &&
    !event.defaultPrevented &&
    !event.isComposing &&
    event.keyCode !== 229 &&
    !(event.target instanceof Element && event.target.closest('[data-shortcut-recorder]')) &&
    event.metaKey !== event.ctrlKey &&
    !event.altKey &&
    event.shiftKey === binding.shift &&
    event.key.toLowerCase() === binding.key,
  );
}
