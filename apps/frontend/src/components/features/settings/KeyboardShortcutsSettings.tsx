'use client';

import { useRef, useState } from 'react';
import { useAuth, useT } from '@/components/providers';
import {
  DEFAULT_KEYBOARD_SHORTCUTS,
  SHORTCUT_ACTIONS,
  SHORTCUT_LABELS,
  chordLabel,
  hasPrimaryModifier,
  isReservedShortcut,
  shortcutConflict,
  shortcutLabel,
  type KeyboardShortcuts,
  type ShortcutAction,
  type ShortcutBinding,
} from '@/lib/keyboardShortcuts';
import { useApplePlatform, useKeyboardShortcuts } from './keyboardShortcutPreferences';

function builtInShortcuts(apple: boolean) {
  return [
    ['send', 'Enter'],
    ['newline', 'Shift + Enter'],
    ['sendNow', chordLabel('Enter', apple)],
    ['stop', 'Esc'],
  ] as const;
}

/** The primary modifier by the name its key carries. */
function modifierName(apple: boolean): string {
  return apple ? 'Command' : 'Ctrl';
}

function ShortcutRow({
  action,
  shortcuts,
  disabled,
  onChange,
}: {
  action: ShortcutAction;
  shortcuts: KeyboardShortcuts;
  disabled: boolean;
  onChange: (binding: ShortcutBinding | null) => void;
}) {
  const t = useT();
  const apple = useApplePlatform();
  const [recording, setRecording] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const label = t(SHORTCUT_LABELS[action]);
  const binding = shortcuts[action];
  const errorId = `shortcut-${action}-error`;

  function finish() {
    setRecording(false);
    setError(null);
    requestAnimationFrame(() => trigger.current?.focus());
  }

  return (
    <div className="flex flex-col gap-2 border-t border-border py-3 sm:flex-row sm:items-center sm:justify-between sm:gap-4">
      <div className="min-w-0">
        <h2 className="text-sm font-medium text-foreground">{label}</h2>
        <p className="mt-1 text-xs leading-5 text-muted">{t(`settings.shortcuts.${action}Help`)}</p>
      </div>
      <div className="shrink-0 sm:max-w-[60%]">
        <div className="flex flex-wrap items-center gap-2">
          {recording ? (
            <input
              autoFocus
              readOnly
              data-shortcut-recorder
              aria-label={t('settings.shortcuts.record', { action: label })}
              aria-describedby={`shortcut-recording-help${error ? ` ${errorId}` : ''}`}
              aria-invalid={Boolean(error)}
              value={t('settings.shortcuts.pressKeys')}
              className="ui-focus-ring min-h-11 w-56 max-w-full rounded-lg border border-crimson bg-surface-muted px-3 text-sm text-foreground"
              onBlur={() => {
                setRecording(false);
                setError(null);
              }}
              onKeyDown={(event) => {
                if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
                if (event.key === 'Tab') return;
                event.preventDefault();
                event.stopPropagation();
                if (event.key === 'Escape') {
                  finish();
                  return;
                }
                if (event.repeat || ['Meta', 'Control', 'Shift', 'Alt'].includes(event.key)) return;
                if (!hasPrimaryModifier(event) || event.altKey || !/^[a-z]$/i.test(event.key)) {
                  setError(t('settings.shortcuts.invalid', { modifier: modifierName(apple) }));
                  return;
                }
                const next = { key: event.key.toLowerCase(), shift: event.shiftKey };
                if (isReservedShortcut(next)) {
                  setError(t('settings.shortcuts.reserved'));
                  return;
                }
                const conflict = shortcutConflict(shortcuts, action, next);
                if (conflict) {
                  setError(
                    t('settings.shortcuts.conflict', { action: t(SHORTCUT_LABELS[conflict]) }),
                  );
                  return;
                }
                onChange(next);
                finish();
              }}
            />
          ) : (
            <button
              ref={trigger}
              type="button"
              disabled={disabled}
              aria-label={t('settings.shortcuts.change', { action: label })}
              aria-describedby={`shortcut-${action}-binding`}
              onClick={() => setRecording(true)}
              className="ui-button ui-button-secondary min-h-11 w-56 max-w-full justify-center disabled:opacity-50"
            >
              {binding ? (
                <kbd id={`shortcut-${action}-binding`} className="font-mono text-xs">
                  {shortcutLabel(binding, apple)}
                </kbd>
              ) : (
                <span id={`shortcut-${action}-binding`} className="text-sm text-muted">
                  {t('settings.shortcuts.disabled')}
                </span>
              )}
            </button>
          )}
          <button
            type="button"
            disabled={disabled || !binding}
            aria-label={t('settings.shortcuts.disableAction', { action: label })}
            onClick={() => {
              onChange(null);
              finish();
            }}
            className="ui-focus-ring min-h-11 rounded-lg px-3 text-xs text-muted hover:bg-surface-hover hover:text-foreground disabled:cursor-not-allowed disabled:opacity-40"
          >
            {t('settings.shortcuts.disable')}
          </button>
        </div>
        {error ? (
          <p
            id={errorId}
            role="alert"
            className="mt-2 max-w-sm text-xs text-red-600 dark:text-red-400"
          >
            {error}
          </p>
        ) : null}
      </div>
    </div>
  );
}

export function KeyboardShortcutsSettings() {
  const { state } = useAuth();
  const userId = state.user?.id;
  const t = useT();
  const apple = useApplePlatform();
  const [shortcuts, update] = useKeyboardShortcuts(userId);
  const customized = JSON.stringify(shortcuts) !== JSON.stringify(DEFAULT_KEYBOARD_SHORTCUTS);

  return (
    <section
      aria-label={t('settings.shortcuts.title')}
      className="ui-card mt-6 bg-surface p-3 sm:p-4"
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <p id="shortcut-recording-help" className="text-xs leading-5 text-muted">
          {t('settings.shortcuts.recordingHelp', { modifier: modifierName(apple) })}
        </p>
        <button
          type="button"
          disabled={!userId || !customized}
          onClick={() => update(DEFAULT_KEYBOARD_SHORTCUTS)}
          className="ui-button ui-button-secondary min-h-11 text-xs disabled:opacity-50"
        >
          {t('settings.shortcuts.reset')}
        </button>
      </div>
      {SHORTCUT_ACTIONS.map((action) => (
        <ShortcutRow
          key={`${userId}:${action}`}
          action={action}
          shortcuts={shortcuts}
          disabled={!userId}
          onChange={(binding) => update({ ...shortcuts, [action]: binding })}
        />
      ))}
      <div className="border-t border-border pt-3">
        <h2 className="text-xs font-semibold text-foreground">{t('settings.shortcuts.builtIn')}</h2>
        <dl className="mt-2 grid gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
          {builtInShortcuts(apple).map(([action, keys]) => (
            <div key={action} className="flex items-start justify-between gap-3">
              <dt className="leading-5 text-muted">{t(`settings.shortcuts.${action}`)}</dt>
              <dd className="shrink-0 rounded border border-border bg-surface-muted px-1.5 py-0.5 text-foreground">
                <kbd className="font-mono">{keys}</kbd>
              </dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}
