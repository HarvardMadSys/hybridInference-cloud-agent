'use client';

import { useCallback, useEffect, useState } from 'react';
import toast from 'react-hot-toast';

import { useT } from '@/components/providers/LocaleProvider';
import { formatBytes } from '@/lib/attachmentLimits';
import type { PendingMessageApi } from '@/lib/api/pendingMessages';

import { isComposerSubmit } from './composerKeys';
import type { PendingQueue } from './usePendingMessages';

type Translate = ReturnType<typeof useT>;

interface EditorState {
  draft: string;
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

const BADGE_CLASS = {
  queued: 'bg-gray-100 text-gray-500 dark:bg-white/5 dark:text-gray-400',
  paused: 'bg-amber-50 text-amber-800 dark:bg-amber-500/10 dark:text-amber-300',
  editing: 'bg-amber-50 text-amber-700 dark:bg-amber-500/10 dark:text-amber-400',
  refused: 'bg-red-50 text-red-700 dark:bg-red-500/10 dark:text-red-400',
  sending: 'bg-sky-50 text-sky-700 dark:bg-sky-500/10 dark:text-sky-400',
} as const;

/**
 * The one word a card leads with. A held message is either being edited or
 * refused; a waiting one in a paused queue is not waiting for any turn, so it
 * says so rather than promising to go when this one ends.
 */
function badgeFor(message: PendingMessageApi, paused: boolean): keyof typeof BADGE_CLASS {
  if (message.state === 'sending') return 'sending';
  if (message.state === 'held') return message.last_error ? 'refused' : 'editing';
  return paused ? 'paused' : 'queued';
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

function without<T>(record: Record<string, T>, key: string): Record<string, T> {
  const next = { ...record };
  delete next[key];
  return next;
}

/**
 * The messages waiting behind the running turn, above the composer.
 *
 * Not a second transcript: a message leaves this list when the server sends
 * it, and appears in the conversation as the turn it became. Everything here
 * reads the server's state and writes through it — opening the editor holds
 * the message (the queue will not send past it), saving or cancelling lets it
 * go again. A change another tab made first is shown, never overwritten, and
 * the owner's draft is kept on screen when that happens.
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
  const [editors, setEditors] = useState<Record<string, EditorState>>({});
  const [notice, setNotice] = useState<string | null>(null);
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
        [message.id]: { draft: message.prompt, revision, saving: false, note: null },
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

  if (!queue.messages.length && !orphans.length && !queue.pausedReason) return null;

  const card =
    'rounded-lg border border-gray-200/80 bg-white px-3 py-2 shadow-[0_10px_24px_-22px_rgba(17,24,39,0.6)] dark:border-white/10 dark:bg-gray-900';
  const action =
    'rounded-md px-1.5 py-1 text-[11px] text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-800 disabled:cursor-not-allowed disabled:opacity-40 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100';

  return (
    <section aria-label={t('queue.region')} className="mb-1.5" data-testid="pending-message-queue">
      {queue.messages.length ? (
        <div className="flex items-center gap-2 px-1 pb-1">
          <span className="text-[11px] font-medium text-gray-500 dark:text-gray-400">
            {t('queue.heading')}
          </span>
          <span className="ml-auto text-[11px] text-gray-400 dark:text-gray-500">
            {queue.messages.length === 1
              ? t('queue.countOne')
              : t('queue.countMany', { count: String(queue.messages.length) })}
          </span>
        </div>
      ) : null}
      {queue.pausedReason ? (
        <div
          role="status"
          className="mb-1 flex flex-wrap items-center gap-2 rounded-md bg-amber-50 px-2 py-1 text-[11px] text-amber-800 dark:bg-amber-500/10 dark:text-amber-300"
        >
          <span>{t(`queue.paused.${queue.pausedReason}`)}</span>
          <button
            type="button"
            data-action="resume"
            aria-label={t('queue.resumeLabel')}
            disabled={queue.resuming || disabled}
            onClick={() => void resume()}
            className="rounded-md border border-amber-300/80 bg-white/70 px-2 py-0.5 font-medium transition-colors hover:bg-white disabled:cursor-not-allowed disabled:opacity-50 dark:border-amber-500/30 dark:bg-amber-500/10 dark:hover:bg-amber-500/20"
          >
            {t('queue.resume')}
          </button>
        </div>
      ) : null}
      <ul className="max-h-56 space-y-1.5 overflow-y-auto pr-0.5">
        {queue.messages.map((message) => {
          const editor = editors[message.id];
          const badge = badgeFor(message, queue.pausedReason !== null);
          const busy = queue.busyIds.has(message.id);
          const locked = message.state === 'sending' || disabled;
          const files = attachmentSummary(message, t);
          return (
            <li key={message.id} data-message-id={message.id} className={card}>
              <div className="flex items-start gap-2">
                <span
                  className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${BADGE_CLASS[badge]}`}
                >
                  {t(`queue.state.${badge}`)}
                </span>
                <p className="line-clamp-3 min-w-0 flex-1 whitespace-pre-wrap break-words text-[13px] leading-relaxed text-gray-700 dark:text-gray-300">
                  {message.prompt}
                </p>
              </div>
              {files ? (
                <p className="mt-1 truncate text-[11px] text-gray-400 dark:text-gray-500">
                  {files}
                </p>
              ) : null}
              {message.state === 'held' && message.last_error ? (
                <p className="mt-1 text-[11px] text-red-600 dark:text-red-400">
                  {message.last_error}
                </p>
              ) : null}
              {editor ? (
                <div className="mt-2 border-t border-gray-100 pt-2 dark:border-white/5">
                  <textarea
                    autoFocus
                    rows={2}
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
                    className="w-full resize-none rounded-md border border-gray-200 bg-white px-2 py-1.5 text-[13px] leading-relaxed text-gray-900 focus:border-crimson/30 focus:outline-none focus:ring-2 focus:ring-crimson/[0.08] dark:border-white/10 dark:bg-gray-950 dark:text-gray-50"
                  />
                  <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                    <button
                      type="button"
                      data-action="save"
                      disabled={editor.saving}
                      onClick={() => void settle(message, editor.draft)}
                      className="rounded-md bg-gray-900 px-2 py-1 text-[11px] font-medium text-white transition-colors hover:bg-gray-700 disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900 dark:hover:bg-white"
                    >
                      {editor.saving ? t('queue.saving') : t('queue.save')}
                    </button>
                    <button
                      type="button"
                      data-action="cancel"
                      disabled={editor.saving}
                      onClick={() => void settle(message, null)}
                      className={action}
                    >
                      {t('queue.cancelEdit')}
                    </button>
                    <span className="text-[11px] text-gray-400 dark:text-gray-500">
                      {t('queue.editHint')}
                    </span>
                  </div>
                  {editor.note ? (
                    <p role="alert" className="mt-1 text-[11px] text-red-600 dark:text-red-400">
                      {editor.note}
                    </p>
                  ) : null}
                </div>
              ) : (
                <div className="mt-1.5 flex items-center gap-1">
                  <button
                    type="button"
                    data-action="edit"
                    aria-label={t('queue.editLabel')}
                    disabled={locked || busy}
                    onClick={() => void openEditor(message)}
                    className={action}
                  >
                    {t('queue.edit')}
                  </button>
                  <button
                    type="button"
                    data-action="delete"
                    aria-label={t('queue.deleteLabel')}
                    disabled={locked || busy}
                    onClick={() => void remove(message)}
                    className={action}
                  >
                    {t('queue.delete')}
                  </button>
                </div>
              )}
            </li>
          );
        })}
        {orphans.map((id) => (
          <li key={id} data-message-id={id} className={card}>
            <p className="text-[11px] text-gray-500 dark:text-gray-400">{t('queue.gone')}</p>
            <textarea
              rows={2}
              readOnly
              value={editors[id].draft}
              aria-label={t('queue.editLabel')}
              className="mt-1 w-full resize-none rounded-md border border-gray-200 bg-gray-50 px-2 py-1.5 text-[13px] text-gray-600 dark:border-white/10 dark:bg-gray-950 dark:text-gray-300"
            />
            <button
              type="button"
              onClick={() => setEditors((current) => without(current, id))}
              className={action}
            >
              {t('queue.dismiss')}
            </button>
          </li>
        ))}
      </ul>
      {notice ? (
        <p role="alert" className="px-1 pt-1 text-[11px] text-red-600 dark:text-red-400">
          {notice}
        </p>
      ) : null}
    </section>
  );
}
