'use client';

import { formatBytes } from '@/lib/attachmentLimits';
import { useT } from '@/components/providers';
import type { AttachmentEntry } from './useAttachments';

/**
 * What is attached to this message, inside the composer body.
 *
 * Not in the control row. The row answers "how does this run" — one value per
 * control, always present. An attached file is part of what the user is
 * sending, so it lives with the text, where it can be read and removed one by
 * one.
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

function RemoveIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={2}
      aria-hidden="true"
      className="h-3 w-3"
    >
      <path strokeLinecap="round" d="M6 6l12 12M18 6L6 18" />
    </svg>
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
  const t = useT();
  if (entries.length === 0) return null;
  const groups = groupAttachments(entries);
  return (
    <div className="px-5 pb-3">
      <ul className="flex flex-wrap gap-1.5">
        {groups.map((group) => {
          const entry = group.members[0]!;
          const rejected = entry.status === 'rejected' && !group.folder;
          const uploading = group.members.some((member) => member.status === 'uploading');
          return (
            <li
              key={group.key}
              className={`inline-flex max-w-full items-center gap-1.5 rounded-md border px-2 py-1 ${
                rejected
                  ? 'border-amber-300 bg-amber-50 dark:border-amber-500/40 dark:bg-amber-500/10'
                  : 'border-gray-200 bg-gray-50 dark:border-white/10 dark:bg-white/5'
              }`}
            >
              <span
                className={
                  rejected
                    ? 'text-amber-700 dark:text-amber-400'
                    : 'text-gray-400 dark:text-gray-500'
                }
              >
                {group.folder ? <FolderIcon /> : <FileIcon />}
              </span>
              <span
                title={group.label}
                className={`truncate font-mono text-[11.5px] ${
                  rejected
                    ? 'text-amber-800 dark:text-amber-300'
                    : 'text-gray-700 dark:text-gray-200'
                }`}
              >
                {group.label}
              </span>
              <span
                className={`shrink-0 text-[11px] ${
                  rejected
                    ? 'text-amber-700 dark:text-amber-400'
                    : 'text-gray-400 dark:text-gray-500'
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
        })}
      </ul>
    </div>
  );
}
