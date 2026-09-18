'use client';

import { useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { SkillIcon } from './skills/SkillDialog';

// The composer's one attach point.
//
// The control row used to carry Skills next to the repository, branch, agent,
// model and reasoning pickers. Those five each take exactly one value and are
// always meaningful, so they are always visible. Skills is zero-or-more and
// most tasks carry none — a different kind of choice wearing the same kind of
// control. This menu is where that second kind lives.
//
// The two upload items open the operating system's own file picker rather than
// a dialog of our own. A picker we drew would be a second thing to learn, a
// second thing to make accessible, and a second thing to keep working; the
// native one is already all three. They are two items rather than one because
// a file input can select files or a directory, never both: the directory
// variant needs `webkitdirectory`, which is set below rather than written as a
// JSX prop because React's typings do not carry it.

function PlusIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.9}
      aria-hidden="true"
      className="h-4 w-4"
    >
      <path strokeLinecap="round" d="M12 5v14M5 12h14" />
    </svg>
  );
}

function DocumentIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden="true"
      className="h-[17px] w-[17px] shrink-0"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M14 3v5h5M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z"
      />
    </svg>
  );
}

function DirectoryIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden="true"
      className="h-[17px] w-[17px] shrink-0"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M3 7a2 2 0 0 1 2-2h3.6l2 2H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"
      />
    </svg>
  );
}

const ITEM =
  'flex w-full items-center gap-2.5 rounded-lg px-2.5 py-2 text-left text-[13.5px] font-medium text-gray-900 hover:bg-gray-50 focus:bg-gray-50 focus:outline-none dark:text-gray-100 dark:hover:bg-white/5 dark:focus:bg-white/5';

export function ComposerPlusMenu({
  onFiles,
  onOpenSkills,
  skillSummary,
  disabled = false,
}: {
  onFiles: (files: File[]) => void;
  onOpenSkills: () => void;
  /** What the Skills row reports on its right: a count, or a pending note. */
  skillSummary: string;
  disabled?: boolean;
}) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const filePicker = useRef<HTMLInputElement>(null);
  const directoryPicker = useRef<HTMLInputElement>(null);

  // Not a JSX prop: `webkitdirectory` is absent from React's input typings, and
  // the lowercase spelling React would emit for an unknown prop is not the one
  // browsers honour.
  useEffect(() => {
    const input = directoryPicker.current;
    if (!input) return;
    input.setAttribute('webkitdirectory', '');
    input.setAttribute('directory', '');
  }, []);

  useEffect(() => {
    if (!open) return undefined;
    menu.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    function onPointerDown(event: MouseEvent) {
      const target = event.target as Node;
      if (menu.current?.contains(target) || trigger.current?.contains(target)) return;
      setOpen(false);
    }
    document.addEventListener('mousedown', onPointerDown);
    return () => document.removeEventListener('mousedown', onPointerDown);
  }, [open]);

  function close(restoreFocus = true) {
    setOpen(false);
    if (restoreFocus) trigger.current?.focus();
  }

  function moveFocus(step: 1 | -1) {
    const items = Array.from(
      menu.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [],
    );
    if (items.length === 0) return;
    const at = items.indexOf(document.activeElement as HTMLElement);
    const next = at < 0 ? 0 : (at + step + items.length) % items.length;
    items[next]?.focus();
  }

  function take(event: React.ChangeEvent<HTMLInputElement>) {
    const picked = Array.from(event.target.files ?? []);
    // Clearing lets the same file be picked again — without it the input's
    // value is unchanged and no second `change` event fires.
    event.target.value = '';
    if (picked.length) onFiles(picked);
  }

  return (
    <>
      <button
        ref={trigger}
        type="button"
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t('attach.open')}
        title={t('attach.open')}
        onClick={() => setOpen((was) => !was)}
        onKeyDown={(event) => {
          if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
          event.preventDefault();
          setOpen(true);
        }}
        className={`inline-flex h-[30px] w-[30px] shrink-0 items-center justify-center rounded-lg border transition-colors disabled:cursor-not-allowed disabled:opacity-50 ${
          open
            ? 'border-gray-300 bg-gray-100 text-gray-900 dark:border-white/20 dark:bg-white/10 dark:text-gray-100'
            : 'border-gray-200 bg-white text-gray-600 hover:bg-gray-50 dark:border-white/10 dark:bg-transparent dark:text-gray-300 dark:hover:bg-white/5'
        }`}
      >
        <PlusIcon />
      </button>

      <input
        ref={filePicker}
        type="file"
        multiple
        hidden
        aria-hidden="true"
        tabIndex={-1}
        onChange={take}
      />
      <input
        ref={directoryPicker}
        type="file"
        multiple
        hidden
        aria-hidden="true"
        tabIndex={-1}
        onChange={take}
      />

      {open ? (
        <div
          ref={menu}
          role="menu"
          aria-label={t('attach.open')}
          onKeyDown={(event) => {
            if (event.key === 'Escape') {
              event.preventDefault();
              close();
            } else if (event.key === 'ArrowDown') {
              event.preventDefault();
              moveFocus(1);
            } else if (event.key === 'ArrowUp') {
              event.preventDefault();
              moveFocus(-1);
            }
          }}
          className="absolute bottom-[calc(100%+6px)] left-0 z-50 w-[272px] rounded-xl border border-gray-200 bg-white p-1.5 shadow-lg dark:border-white/10 dark:bg-gray-900"
        >
          <button
            type="button"
            role="menuitem"
            className={ITEM}
            onClick={() => {
              close(false);
              filePicker.current?.click();
            }}
          >
            <DocumentIcon />
            {t('attach.uploadFiles')}
          </button>
          <button
            type="button"
            role="menuitem"
            className={ITEM}
            onClick={() => {
              close(false);
              directoryPicker.current?.click();
            }}
          >
            <DirectoryIcon />
            {t('attach.uploadDirectory')}
          </button>
          <div className="my-1 h-px bg-gray-100 dark:bg-white/5" />
          <button
            type="button"
            role="menuitem"
            className={ITEM}
            onClick={() => {
              close(false);
              onOpenSkills();
            }}
          >
            <SkillIcon className="h-[17px] w-[17px] shrink-0" />
            <span className="flex-1">{t('attach.skills')}</span>
            <span className="text-xs font-normal text-gray-500 dark:text-gray-400">
              {skillSummary}
            </span>
          </button>
        </div>
      ) : null}
    </>
  );
}
