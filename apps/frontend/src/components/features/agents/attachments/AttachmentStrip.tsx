'use client';

import { useState } from 'react';
import { formatBytes } from '@/lib/attachmentLimits';
import { useT } from '@/components/providers';
import type { AttachmentEntry } from './useAttachments';

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

/** The chip a folder, a plain file, or an unreadable image falls back to. */
function FileChip({
  group,
  onRemove,
  disabled,
}: {
  group: Group;
  onRemove: (key: string) => void;
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
        onClick={() => group.members.forEach((member) => onRemove(member.key))}
        className="shrink-0 rounded text-gray-400 hover:text-gray-700 disabled:opacity-50 dark:text-gray-500 dark:hover:text-gray-200"
      >
        <RemoveIcon />
      </button>
    </li>
  );
}

/**
 * One picture, at thumbnail size.
 *
 * It is decoded from the local object URL the composer made when the file
 * arrived, so it is on screen before the upload lands and stays readable
 * afterwards. Bytes that turn out not to be a decodable image drop back to the
 * plain chip: the entry is still valid and still sends, only its preview lied.
 */
function ImageThumb({
  group,
  onRemove,
  disabled,
}: {
  group: Group;
  onRemove: (key: string) => void;
  disabled: boolean;
}) {
  const t = useT();
  const [broken, setBroken] = useState(false);
  const entry = group.members[0]!;
  const uploading = entry.status === 'uploading';
  if (broken) return <FileChip group={group} onRemove={onRemove} disabled={disabled} />;
  return (
    <li className="relative">
      <div className="relative h-14 w-14 overflow-hidden rounded-lg border border-border bg-surface-muted">
        {/* The remove button carries the accessible name; the picture itself
            is described by the upload's own name. */}
        {/* eslint-disable-next-line @next/next/no-img-element */}
        <img
          src={entry.previewUrl}
          alt={entry.path}
          title={entry.path}
          onError={() => setBroken(true)}
          className={`h-full w-full object-cover ${
            uploading ? 'opacity-60' : 'opacity-100'
          } transition-opacity`}
        />
        {uploading ? (
          <span
            aria-hidden="true"
            className="pointer-events-none absolute inset-x-0 bottom-0 bg-surface/80 px-1 py-0.5 text-center text-[10px] leading-3 text-muted"
          >
            {t('attach.uploading')}
          </span>
        ) : null}
        <button
          type="button"
          disabled={disabled}
          aria-label={t('attach.remove', { name: entry.path })}
          onClick={() => onRemove(entry.key)}
          className="absolute right-1 top-1 rounded-full bg-gray-900/60 p-0.5 text-white shadow-sm transition-colors hover:bg-gray-900/85 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-focus focus-visible:ring-offset-1 focus-visible:ring-offset-surface disabled:opacity-50"
        >
          <RemoveIcon className="h-3 w-3" />
        </button>
      </div>
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
  if (entries.length === 0) return null;
  return (
    <div className="px-4 pt-3">
      <ul className="flex flex-wrap items-center gap-1.5">
        {groupAttachments(entries).map((group) =>
          previewable(group) ? (
            <ImageThumb key={group.key} group={group} onRemove={onRemove} disabled={disabled} />
          ) : (
            <FileChip key={group.key} group={group} onRemove={onRemove} disabled={disabled} />
          ),
        )}
      </ul>
    </div>
  );
}
