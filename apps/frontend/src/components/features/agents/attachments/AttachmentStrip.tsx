'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { formatBytes } from '@/lib/attachmentLimits';
import { useT } from '@/components/providers';
import type { AttachmentEntry } from './useAttachments';
import { useCollapseHeight } from './useCollapseHeight';

/**
 * What is attached to this message, above the text inside the composer body.
 *
 * Not in the control row. The row answers "how does this run" — one value per
 * control, always present. An attached file is part of what the user is
 * sending, so it lives with the text, where it can be read and removed one by
 * one. A picture is drawn as the picture: a pasted screenshot is what the agent
 * will look at, and its generated upload name says nothing a reader wants.
 */

/** A folder upload arrives as one entry per file; the strip shows the folder. */
interface Group {
  key: string;
  label: string;
  bytes: number;
  members: AttachmentEntry[];
  folder: boolean;
}

export function groupAttachments(entries: AttachmentEntry[]): Group[] {
  const groups: Group[] = [];
  const byFolder = new Map<string, Group>();
  for (const entry of entries) {
    const slash = entry.path.indexOf('/');
    // A rejected file is always its own chip: the reason belongs beside the
    // file that caused it, and folding it into "specs/ · 12 files" would hide
    // the one thing the user needs to read.
    if (slash < 0 || entry.status === 'rejected') {
      groups.push({
        key: entry.key,
        label: entry.path,
        bytes: entry.size,
        members: [entry],
        folder: false,
      });
      continue;
    }
    const name = `${entry.path.slice(0, slash)}/`;
    const existing = byFolder.get(name);
    if (existing) {
      existing.members.push(entry);
      existing.bytes += entry.size;
      continue;
    }
    const group: Group = {
      key: `dir:${name}`,
      label: name,
      bytes: entry.size,
      members: [entry],
      folder: true,
    };
    byFolder.set(name, group);
    groups.push(group);
  }
  return groups;
}

/**
 * Whether this group is one picture with a preview still alive.
 *
 * A rejected image is a chip instead: the reason it was refused is the part
 * worth reading, and there is no local preview left to draw anyway.
 */
function previewable(group: Group): boolean {
  const entry = group.members[0];
  return (
    entry !== undefined && !group.folder && entry.status !== 'rejected' && Boolean(entry.previewUrl)
  );
}

function FolderIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden="true"
      className="h-3.5 w-3.5 shrink-0"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M3 7a2 2 0 0 1 2-2h3.6l2 2H19a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z"
      />
    </svg>
  );
}

function FileIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.6}
      aria-hidden="true"
      className="h-3.5 w-3.5 shrink-0"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M14 3v5h5M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Z"
      />
    </svg>
  );
}

function RemoveIcon({ className = 'h-3 w-3' }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      aria-hidden="true"
      className={className}
    >
      <path strokeLinecap="round" d="M6 6l12 12M18 6L6 18" />
    </svg>
  );
}

/** Remove every file a strip item stands for; the button is where focus was. */
type Remove = (group: Group, button: HTMLButtonElement) => void;

/** The chip a folder, a plain file, or an unreadable image falls back to. */
function FileChip({
  group,
  onRemove,
  disabled,
}: {
  group: Group;
  onRemove: Remove;
  disabled: boolean;
}) {
  const t = useT();
  const entry = group.members[0]!;
  const rejected = entry.status === 'rejected' && !group.folder;
  const uploading = group.members.some((member) => member.status === 'uploading');
  return (
    <li
      className={`inline-flex max-w-full items-center gap-1.5 rounded-md border px-2 py-1 ${
        rejected
          ? 'border-amber-300 bg-amber-50 dark:border-amber-500/40 dark:bg-amber-500/10'
          : 'border-gray-200 bg-gray-50 dark:border-white/10 dark:bg-white/5'
      }`}
    >
      <span
        className={
          rejected ? 'text-amber-700 dark:text-amber-400' : 'text-gray-400 dark:text-gray-500'
        }
      >
        {group.folder ? <FolderIcon /> : <FileIcon />}
      </span>
      <span
        title={group.label}
        className={`truncate font-mono text-[11.5px] ${
          rejected ? 'text-amber-800 dark:text-amber-300' : 'text-gray-700 dark:text-gray-200'
        }`}
      >
        {group.label}
      </span>
      <span
        className={`shrink-0 text-[11px] ${
          rejected ? 'text-amber-700 dark:text-amber-400' : 'text-gray-400 dark:text-gray-500'
        }`}
      >
        {rejected
          ? entry.error
          : group.folder
            ? t('attach.fileCount', { count: group.members.length })
            : uploading
              ? t('attach.uploading')
              : formatBytes(group.bytes)}
      </span>
      <button
        type="button"
        disabled={disabled}
        aria-label={t('attach.remove', { name: group.label })}
        data-attachment-remove=""
        onClick={(event) => onRemove(group, event.currentTarget)}
        className="shrink-0 rounded text-gray-400 hover:text-gray-700 disabled:opacity-50 dark:text-gray-500 dark:hover:text-gray-200"
      >
        <RemoveIcon />
      </button>
    </li>
  );
}

/**
 * One picture, large enough to recognise what is in it.
 *
 * It is decoded from the local object URL the composer made when the file
 * arrived, so it is on screen before the upload lands and stays readable
 * afterwards. Bytes that turn out not to be a decodable image are reported
 * through `onBroken`, and the strip draws them as a plain chip: the entry is
 * still valid and still sends, only its preview lied.
 */
function ImageThumb({
  group,
  onRemove,
  onBroken,
  disabled,
}: {
  group: Group;
  onRemove: Remove;
  onBroken: (key: string) => void;
  disabled: boolean;
}) {
  const t = useT();
  const entry = group.members[0]!;
  const uploading = entry.status === 'uploading';
  return (
    <li className="relative h-28 w-28 shrink-0 overflow-hidden rounded-xl border border-border bg-surface-muted sm:h-32 sm:w-32">
      {/* The picture is named by its upload path, as its remove button is. */}
      {/* eslint-disable-next-line @next/next/no-img-element */}
      <img
        src={entry.previewUrl}
        alt={entry.path}
        title={entry.path}
        decoding="async"
        draggable={false}
        onError={() => onBroken(group.key)}
        // A screenshot's heading is at its top; keep that in view when cropping.
        className={`h-full w-full object-cover object-top transition-opacity ${
          uploading ? 'opacity-60' : 'opacity-100'
        }`}
      />
      {uploading ? (
        // Read out as well as shown: a chip says it in text, so must this.
        <span className="pointer-events-none absolute inset-x-0 bottom-0 bg-surface/85 px-1.5 py-1 text-center text-[11px] leading-4 text-muted">
          {t('attach.uploading')}
        </span>
      ) : null}
      <button
        type="button"
        disabled={disabled}
        aria-label={t('attach.remove', { name: entry.path })}
        data-attachment-remove=""
        onClick={(event) => onRemove(group, event.currentTarget)}
        className="ui-focus-ring absolute right-1.5 top-1.5 flex h-6 w-6 items-center justify-center rounded-full bg-gray-900/70 text-white shadow-sm ring-1 ring-white/40 transition-colors hover:bg-gray-900/90 disabled:opacity-50 [@media(pointer:coarse)]:h-8 [@media(pointer:coarse)]:w-8"
      >
        <RemoveIcon className="h-3.5 w-3.5" />
      </button>
    </li>
  );
}

export function AttachmentStrip({
  entries,
  onRemove,
  disabled = false,
}: {
  entries: AttachmentEntry[];
  onRemove: (key: string) => void;
  disabled?: boolean;
}) {
  // Where keyboard focus goes once a removed item's button is gone: the
  // button now in its place, the one before it, or — when nothing is left —
  // the message box this strip sits above. Losing focus to the page would
  // leave the next key press (Escape stops a running task) aimed at the page.
  const pending = useRef<{ scope: HTMLElement; index: number } | null>(null);
  const [broken, setBroken] = useState<ReadonlySet<string>>(() => new Set());
  const markBroken = useCallback(
    (key: string) => setBroken((old) => (old.has(key) ? old : new Set(old).add(key))),
    [],
  );
  // Pictures first, as one row of their own, then everything else as chips:
  // mixing a short chip into a row of tall pictures leaves both ragged.
  const groups = groupAttachments(entries);
  const { frameRef, contentRef, mounted } = useCollapseHeight(groups.length);
  const shown = (group: Group) => previewable(group) && !broken.has(group.key);
  const pictures = groups.filter(shown);
  const files = groups.filter((group) => !shown(group));

  useEffect(() => {
    const next = pending.current;
    pending.current = null;
    if (!next) return;
    // Only a caret that has nowhere to be is moved. A user who has already put
    // focus somewhere else since the click keeps it.
    const active = document.activeElement;
    if (active && active !== document.body && active.isConnected) return;
    const buttons = next.scope.querySelectorAll<HTMLButtonElement>('[data-attachment-remove]');
    const target =
      buttons[Math.min(next.index, buttons.length - 1)] ?? next.scope.querySelector('textarea');
    // The clicked button was focused when it was removed, so the caret is on
    // its way to the page; put it back in the composer before the next key
    // press (Escape there stops a running task) can land.
    target?.focus();
  }, [entries]);
  const remove: Remove = (group, button) => {
    const scope = button.closest('[data-attachment-strip]')?.parentElement;
    if (scope && document.activeElement === button) {
      const buttons = [...scope.querySelectorAll('[data-attachment-remove]')];
      pending.current = { scope, index: buttons.indexOf(button) };
    }
    group.members.forEach((member) => onRemove(member.key));
  };
  // The strip is a block in a column, so adding the first picture or removing
  // the last one used to move the message box by a full row in one frame. One
  // height, animated, keeps that growth and shrinkage continuous; nothing about
  // when an entry is really attached, removed or sent changes. While the space
  // closes there is simply nothing inside it — the entries are already gone.
  if (!mounted) return null;
  return (
    <div ref={frameRef} data-attachment-strip="">
      <div ref={contentRef} className="flex flex-col gap-2 px-4 pt-3">
        {pictures.length ? (
          <ul className="flex flex-wrap gap-2">
            {pictures.map((group) => (
              <ImageThumb
                key={group.key}
                group={group}
                onRemove={remove}
                onBroken={markBroken}
                disabled={disabled}
              />
            ))}
          </ul>
        ) : null}
        {files.length ? (
          <ul className="flex flex-wrap gap-1.5">
            {files.map((group) => (
              <FileChip key={group.key} group={group} onRemove={remove} disabled={disabled} />
            ))}
          </ul>
        ) : null}
      </div>
    </div>
  );
}
