'use client';

import { useCallback, useEffect, useId, useState } from 'react';
import toast from 'react-hot-toast';

import { useT } from '@/components/providers/LocaleProvider';
import { formatBytes } from '@/lib/attachmentLimits';
import type { PendingMessageApi } from '@/lib/api/pendingMessages';

import { isComposerSubmit } from './composerKeys';
import type { PendingQueue } from './usePendingMessages';

type Translate = ReturnType<typeof useT>;

interface EditorState {
  draft: string;
  /** The text the draft started from, to tell when another tab changed it. */
  original: string;
  /**
   * The revision the draft was written against. Saving and cancelling send
   * this, not the card's latest revision: the card keeps reading the server,
   * so its revision would quietly include another tab's change and let this
   * draft overwrite it instead of being refused.
   */
  revision: number;
  saving: boolean;
  /** Why the last save or cancel did not go through; the draft stays. */
  note: string | null;
}

/** Inline, like every other icon in the app; four glyphs do not earn a package. */
function QueueIcon({
  name,
  className = 'h-3.5 w-3.5',
}: {
  name: 'chevron' | 'edit' | 'delete' | 'attachment' | 'send';
  className?: string;
}) {
  return (
    <svg
      className={className}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.8}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {name === 'chevron' ? <path d="m6 9 6 6 6-6" /> : null}
      {name === 'edit' ? (
        <>
          <path d="M4 20h4L18.5 9.5a2.12 2.12 0 0 0-3-3L5 17v3Z" />
          <path d="m13.5 8.5 3 3" />
        </>
      ) : null}
      {name === 'delete' ? <path d="M4 7h16M10 7V4h4v3M6 7l1 13h10l1-13M10 11v5M14 11v5" /> : null}
      {name === 'send' ? <path d="M12 19V5M6 11l6-6 6 6" /> : null}
      {name === 'attachment' ? (
        <path d="m20 11.5-8.2 8.2a5 5 0 0 1-7.1-7.1l8.6-8.6a3.3 3.3 0 0 1 4.7 4.7l-8.6 8.6a1.7 1.7 0 0 1-2.4-2.4l7.9-7.9" />
      ) : null}
    </svg>
  );
}

function attachmentSummary(message: PendingMessageApi, t: Translate): string | null {
  const files = message.attachments ?? [];
  if (!files.length) return null;
  const bytes = files.reduce((total, file) => total + file.size_bytes, 0);
  const names = files
    .slice(0, 2)
    .map((file) => file.path.split('/').filter(Boolean).pop() ?? file.path)
    .join(', ');
  const more = files.length > 2 ? ` +${files.length - 2}` : '';
  const count =
    files.length === 1
      ? t('queue.attachmentsOne')
      : t('queue.attachmentsMany', { count: String(files.length) });
  return `${count} · ${names}${more} · ${formatBytes(bytes)}`;
}

/** Why the running turn did not take a message, in the owner's words. */
export function notDeliveredText(reason: string, t: Translate): string {
  if (reason === 'turn_ended') return t('queue.notDelivered.turnEnded');
  if (reason === 'unreachable') return t('queue.notDelivered.unreachable');
  return t('queue.notDelivered.other');
}

/** Why a message could not even be offered to the running turn. */
function sendNowRefusal(code: string, error: string, t: Translate): string {
  if (code === 'not_running') return t('queue.sendNowRefused.notRunning');
  if (code === 'mid_turn_input_unsupported') return t('queue.sendNowRefused.unsupported');
  if (code === 'send_now_has_attachments') return t('queue.sendNowFiles');
  if (code === 'revision_conflict') return t('queue.conflict');
  return error;
}

/** How long a "not taken" note stays under its message. */
const NOTE_MS = 10_000;

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  const next = { ...record };
  delete next[key];
  return next;
}

const ICON_BUTTON =
  'rounded-md p-1 text-gray-400 transition-colors hover:bg-gray-200/70 hover:text-gray-700 disabled:cursor-not-allowed disabled:opacity-40 dark:text-gray-500 dark:hover:bg-white/10 dark:hover:text-gray-200';
const TEXT_BUTTON =
  'rounded-md px-2 py-0.5 text-[11px] text-gray-600 transition-colors hover:bg-gray-200/70 disabled:opacity-50 dark:text-gray-300 dark:hover:bg-white/10';
const PRIMARY_BUTTON =
  'rounded-md bg-gray-900 px-2 py-0.5 text-[11px] font-medium text-white transition-colors hover:bg-gray-700 disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white';
const OUTLINE_BUTTON =
  'rounded-md border border-gray-200 bg-white px-2 py-0.5 text-[11px] font-medium text-gray-700 transition-colors hover:bg-gray-50 disabled:opacity-50 dark:border-white/15 dark:bg-transparent dark:text-gray-200 dark:hover:bg-white/10';

/**
 * The messages waiting behind the running turn, in a card above the composer.
 *
 * Not a second transcript: a message leaves this list when the server sends
 * it, and appears in the conversation as the turn it became. So a waiting
 * message is one quiet line, and only what needs the owner — an edit, a
 * refusal, a pause — says anything. Everything here reads the server's state
 * and writes through it: opening the editor holds the message (the queue will
 * not send past it), saving or cancelling lets it go again. A change another
 * tab made first is shown, never overwritten, and the owner's draft is kept
 * on screen when that happens.
 */
export function PendingMessageQueue({
  queue,
  disabled = false,
}: {
  queue: PendingQueue;
  /** The conversation cannot take changes (archived, context lost). */
  disabled?: boolean;
}) {
  const t = useT();
  const listId = useId();
  const [collapsed, setCollapsed] = useState(false);
  const [editors, setEditors] = useState<Record<string, EditorState>>({});
  const [notice, setNotice] = useState<string | null>(null);
  /** Per message: why the running turn did not take it just now. */
  const [notes, setNotes] = useState<Record<string, string>>({});
  const known = new Set(queue.messages.map((message) => message.id));
  // A message that was sent or removed while its editor was open: the text is
  // kept so nothing the owner typed disappears with it.
  const orphans = Object.keys(editors).filter((id) => !known.has(id));

  // A pause that is gone takes its refusal with it.
  useEffect(() => {
    if (!queue.pausedReason) setNotice(null);
  }, [queue.pausedReason]);

  const openEditor = useCallback(
    async (message: PendingMessageApi) => {
      setNotice(null);
      const held = message.state === 'held' ? null : await queue.hold(message);
      if (held && !held.ok) {
        setNotice(held.current ? t('queue.conflict') : held.error);
        return;
      }
      const revision = held?.result.pending?.revision ?? message.revision;
      setEditors((current) => ({
        ...current,
        [message.id]: {
          draft: message.prompt,
          original: message.prompt,
          revision,
          saving: false,
          note: null,
        },
      }));
    },
    [queue, t],
  );

  const settle = useCallback(
    async (message: PendingMessageApi, prompt: string | null) => {
      const editor = editors[message.id];
      if (!editor) return;
      if (prompt !== null && !prompt.trim()) {
        setEditors((current) => ({
          ...current,
          [message.id]: { ...editor, note: t('queue.emptyPrompt') },
        }));
        return;
      }
      setEditors((current) => ({ ...current, [message.id]: { ...editor, saving: true } }));
      const written = { ...message, revision: editor.revision };
      const result =
        prompt === null ? await queue.release(written) : await queue.save(written, prompt.trim());
      if (result.ok) {
        setEditors((current) => without(current, message.id));
        return;
      }
      setEditors((current) => ({
        ...current,
        [message.id]: {
          ...(current[message.id] ?? editor),
          // The owner has now been shown what won; saving again is theirs to
          // choose, written over that version rather than refused forever.
          revision: result.current?.revision ?? editor.revision,
          saving: false,
          note:
            result.code === 'revision_conflict'
              ? t('queue.conflict')
              : result.code === 'pending_message_not_found'
                ? t('queue.gone')
                : result.error,
        },
      }));
    },
    [editors, queue, t],
  );

  const remove = useCallback(
    async (message: PendingMessageApi) => {
      setNotice(null);
      const result = await queue.remove(message);
      if (!result.ok) {
        setNotice(result.code === 'revision_conflict' ? t('queue.conflict') : result.error);
        return;
      }
      setEditors((current) => without(current, message.id));
      toast.success(t('queue.removed'));
    },
    [queue, t],
  );

  const resume = useCallback(async () => {
    setNotice(null);
    const result = await queue.resume();
    if (!result.ok) setNotice(result.error);
  }, [queue]);

  const sendNow = useCallback(
    async (message: PendingMessageApi) => {
      setNotice(null);
      setNotes((current) => without(current, message.id));
      const result = await queue.sendNow(message);
      if (!result.ok) {
        setNotice(sendNowRefusal(result.code, result.error, t));
        return;
      }
      const reason = result.result.not_delivered_reason;
      if (reason)
        setNotes((current) => ({ ...current, [message.id]: notDeliveredText(reason, t) }));
    },
    [queue, t],
  );

  // A note says what just happened; it does not stay to become the card's story.
  useEffect(() => {
    const ids = Object.keys(notes);
    if (!ids.length) return undefined;
    const timer = setTimeout(() => setNotes({}), NOTE_MS);
    return () => clearTimeout(timer);
  }, [notes]);

  if (!queue.messages.length && !orphans.length && !queue.pausedReason && !queue.refreshError)
    return null;

  const count = queue.messages.length;
  const listed = count > 0 || orphans.length > 0;

  return (
    <section
      aria-label={t('queue.region')}
      data-testid="pending-message-queue"
      className="mb-2 overflow-hidden rounded-xl border border-gray-200/80 bg-white shadow-[0_8px_24px_-20px_rgba(17,24,39,0.35)] dark:border-white/10 dark:bg-gray-900"
    >
      {queue.refreshError && (
        <p role="status" className="px-3 py-2 text-xs text-amber-700 dark:text-amber-300">
          {queue.loaded ? t('common.refreshFailed') : queue.refreshError}
          <button type="button" onClick={() => void queue.refresh()} className="ml-2 underline">
            {t('common.retry')}
          </button>
        </p>
      )}
      <div className="flex min-h-[30px] items-center gap-2 px-3 pt-1">
        {listed ? (
          <button
            type="button"
            aria-expanded={!collapsed}
            aria-controls={listId}
            onClick={() => setCollapsed((value) => !value)}
            className="-ml-1 flex shrink-0 items-center gap-1 rounded-md px-1 py-0.5 text-xs text-gray-500 transition-colors hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100"
          >
            <QueueIcon
              name="chevron"
              className={`h-3 w-3 transition-transform ${collapsed ? '-rotate-90' : ''}`}
            />
            {count === 1 ? t('queue.countOne') : t('queue.countMany', { count: String(count) })}
          </button>
        ) : null}
        {queue.pausedReason ? (
          <div role="status" className="ml-auto flex min-w-0 items-center gap-2">
            <span className="truncate text-xs text-amber-600 dark:text-amber-400">
              {t(`queue.paused.${queue.pausedReason}`)}
            </span>
            <button
              type="button"
              data-action="resume"
              aria-label={t('queue.resumeLabel')}
              disabled={queue.resuming || disabled}
              onClick={() => void resume()}
              className={`shrink-0 ${OUTLINE_BUTTON}`}
            >
              {t('queue.resume')}
            </button>
          </div>
        ) : null}
      </div>
      {listed && !collapsed ? (
        <ul id={listId} className="max-h-60 overflow-y-auto px-1.5 pb-1.5">
          {queue.messages.map((message) => {
            const editor = editors[message.id];
            const refused = message.state === 'held' && Boolean(message.last_error);
            const busy = queue.busyIds.has(message.id);
            const locked = message.state === 'sending' || disabled;
            const files = attachmentSummary(message, t);
            if (editor) {
              return (
                <li
                  key={message.id}
                  data-message-id={message.id}
                  className="my-0.5 rounded-lg bg-gray-50 px-2 py-2 dark:bg-white/[0.04]"
                >
                  {message.prompt !== editor.original ? (
                    <p className="mb-1.5 flex min-w-0 gap-1.5 text-[11px] text-gray-500 dark:text-gray-400">
                      <span className="shrink-0">{t('queue.latest')}</span>
                      <span className="truncate text-gray-700 dark:text-gray-200">
                        {message.prompt}
                      </span>
                    </p>
                  ) : null}
                  {refused ? (
                    <p className="mb-1.5 text-[11px] text-red-600 dark:text-red-400">
                      {t('queue.state.refused')} · {message.last_error}
                    </p>
                  ) : null}
                  <textarea
                    autoFocus
                    rows={Math.min(6, Math.max(2, editor.draft.split('\n').length))}
                    value={editor.draft}
                    disabled={editor.saving}
                    aria-label={t('queue.editLabel')}
                    onChange={(event) => {
                      const draft = event.target.value;
                      setEditors((current) => ({
                        ...current,
                        [message.id]: { ...(current[message.id] ?? editor), draft },
                      }));
                    }}
                    onKeyDown={(event) => {
                      if (event.key === 'Escape') {
                        event.preventDefault();
                        void settle(message, null);
                        return;
                      }
                      if (!isComposerSubmit(event)) return;
                      event.preventDefault();
                      void settle(message, editor.draft);
                    }}
                    className="w-full resize-none rounded-md border border-gray-200 bg-white px-2 py-1.5 text-xs leading-5 text-gray-900 focus:border-crimson/30 focus:outline-none focus:ring-2 focus:ring-crimson/[0.08] dark:border-white/10 dark:bg-gray-950 dark:text-gray-50"
                  />
                  <div className="mt-1.5 flex items-center gap-1.5">
                    <span className="min-w-0 flex-1 truncate text-[11px] text-gray-400 dark:text-gray-500">
                      {t('queue.editHint')}
                    </span>
                    <button
                      type="button"
                      data-action="cancel"
                      disabled={editor.saving}
                      onClick={() => void settle(message, null)}
                      className={TEXT_BUTTON}
                    >
                      {t('queue.cancelEdit')}
                    </button>
                    <button
                      type="button"
                      data-action="save"
                      disabled={editor.saving}
                      onClick={() => void settle(message, editor.draft)}
                      className={PRIMARY_BUTTON}
                    >
                      {editor.saving ? t('queue.saving') : t('queue.save')}
                    </button>
                  </div>
                  {editor.note ? (
                    <p role="alert" className="mt-1.5 text-[11px] text-red-600 dark:text-red-400">
                      {editor.note}
                    </p>
                  ) : null}
                </li>
              );
            }
            // Held without a refusal is another tab's edit: nothing behind it
            // moves, and that is worth a word. Waiting is not.
            const tag =
              message.state === 'sending'
                ? 'sending'
                : message.state === 'held' && !refused
                  ? 'editing'
                  : null;
            return (
              <li
                key={message.id}
                data-message-id={message.id}
                className="group flex items-start gap-2 rounded-lg px-2 py-1 transition-colors hover:bg-gray-100/70 dark:hover:bg-white/[0.05]"
              >
                <div className="min-w-0 flex-1">
                  <p
                    title={message.prompt}
                    className="truncate text-xs leading-6 text-gray-700 dark:text-gray-200"
                  >
                    {message.prompt}
                  </p>
                  {refused ? (
                    <p className="pb-0.5 text-[11px] leading-4 text-red-600 dark:text-red-400">
                      {t('queue.state.refused')} · {message.last_error}
                    </p>
                  ) : null}
                  {notes[message.id] ? (
                    <p
                      role="status"
                      className="pb-0.5 text-[11px] leading-4 text-amber-700 dark:text-amber-400"
                    >
                      {notes[message.id]}
                    </p>
                  ) : null}
                </div>
                {files ? (
                  <span
                    title={files}
                    className="flex h-6 shrink-0 items-center gap-0.5 text-[11px] text-gray-400 dark:text-gray-500"
                  >
                    <QueueIcon name="attachment" className="h-3 w-3" />
                    {message.attachments?.length}
                  </span>
                ) : null}
                {tag ? (
                  <span
                    title={tag === 'sending' ? t('queue.state.sendingTitle') : undefined}
                    className={`flex h-6 shrink-0 items-center text-[11px] ${tag === 'sending' ? 'text-sky-600 dark:text-sky-400' : 'text-amber-600 dark:text-amber-400'}`}
                  >
                    {t(`queue.state.${tag}`)}
                  </span>
                ) : null}
                <div
                  className={`flex h-6 shrink-0 items-center transition-opacity ${message.state === 'held' ? '' : 'opacity-0 group-hover:opacity-100 group-focus-within:opacity-100 [@media(hover:none)]:opacity-100'}`}
                >
                  <button
                    type="button"
                    data-action="edit"
                    aria-label={t('queue.editLabel')}
                    title={t('queue.edit')}
                    disabled={locked || busy}
                    onClick={() => void openEditor(message)}
                    className={ICON_BUTTON}
                  >
                    <QueueIcon name="edit" className="h-3 w-3" />
                  </button>
                  <button
                    type="button"
                    data-action="delete"
                    aria-label={t('queue.deleteLabel')}
                    title={t('queue.delete')}
                    disabled={locked || busy}
                    onClick={() => void remove(message)}
                    className={ICON_BUTTON}
                  >
                    <QueueIcon name="delete" className="h-3 w-3" />
                  </button>
                  {queue.canSendNow ? (
                    <button
                      type="button"
                      data-action="send-now"
                      aria-label={t('queue.sendNowLabel')}
                      title={files ? t('queue.sendNowFiles') : t('queue.sendNow')}
                      disabled={locked || busy || message.state !== 'queued' || Boolean(files)}
                      onClick={() => void sendNow(message)}
                      className={ICON_BUTTON}
                    >
                      <QueueIcon name="send" className="h-3 w-3" />
                    </button>
                  ) : null}
                </div>
              </li>
            );
          })}
          {orphans.map((id) => (
            <li
              key={id}
              data-message-id={id}
              className="my-0.5 rounded-lg bg-gray-50 px-2 py-2 dark:bg-white/[0.04]"
            >
              <p className="text-[11px] text-gray-500 dark:text-gray-400">{t('queue.gone')}</p>
              <textarea
                rows={2}
                readOnly
                value={editors[id].draft}
                aria-label={t('queue.editLabel')}
                className="mt-1.5 w-full resize-none rounded-md border border-gray-200 bg-white px-2 py-1.5 text-xs leading-5 text-gray-600 dark:border-white/10 dark:bg-gray-950 dark:text-gray-300"
              />
              <div className="mt-1 flex justify-end">
                <button
                  type="button"
                  onClick={() => setEditors((current) => without(current, id))}
                  className={TEXT_BUTTON}
                >
                  {t('queue.dismiss')}
                </button>
              </div>
            </li>
          ))}
        </ul>
      ) : null}
      {notice ? (
        <p role="alert" className="px-3 pb-2 text-[11px] text-red-600 dark:text-red-400">
          {notice}
        </p>
      ) : null}
    </section>
  );
}
