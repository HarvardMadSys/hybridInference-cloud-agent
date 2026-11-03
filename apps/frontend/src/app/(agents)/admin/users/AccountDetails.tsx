'use client';
// What an administrator keeps about an account beside its decisions: a private
// note, the message a suspended account is shown at sign-in, and its recent
// sign-in attempts (which can be purged, once confirmed), as HybridInference's
// user detail.
import { useState, type ReactNode } from 'react';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import { useT } from '@/components/providers/LocaleProvider';
import type { AccountEntry } from '@/lib/api/accounts';
import {
  listLoginEvents,
  purgeLoginEvents,
  saveAccountNotes,
  type LoginEvent,
} from '@/lib/api/accountAdmin';
import { getErrorMessage } from '@/lib/utils/errors';

/** An irreversible step asked once more, with what it will do. */
export function ConfirmBar({
  children,
  confirm,
  onConfirm,
  onCancel,
}: {
  children: ReactNode;
  confirm: string;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const t = useT();
  return (
    <div className="flex flex-wrap items-center gap-2" role="alertdialog">
      <span className="text-sm">{children}</span>
      <button className="ui-button ui-button-danger" onClick={onConfirm}>
        {confirm}
      </button>
      <button className="ui-button ui-button-secondary" onClick={onCancel}>
        {t('adminAccounts.confirm.keep')}
      </button>
    </div>
  );
}

export function AccountNotes({ account, onSaved }: { account: AccountEntry; onSaved: () => void }) {
  const t = useT();
  const [note, setNote] = useState(account.admin_note ?? '');
  const [message, setMessage] = useState(account.suspension_message ?? '');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function save() {
    setBusy(true);
    setError(null);
    try {
      await saveAccountNotes(account, { admin_note: note, suspension_message: message });
      onSaved();
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="space-y-2">
      <label className="block text-sm" htmlFor="admin-note">
        {t('adminAccounts.notes.note')}
      </label>
      <textarea
        id="admin-note"
        className="ui-input w-full"
        rows={2}
        maxLength={2000}
        value={note}
        onChange={(e) => setNote(e.target.value)}
      />
      <label className="block text-sm" htmlFor="suspension-message">
        {t('adminAccounts.notes.message')}
      </label>
      <textarea
        id="suspension-message"
        className="ui-input w-full"
        rows={2}
        maxLength={2000}
        value={message}
        onChange={(e) => setMessage(e.target.value)}
      />
      {error && <AccountNotice error>{error}</AccountNotice>}
      <button className="ui-button ui-button-secondary" disabled={busy} onClick={() => void save()}>
        {t('adminAccounts.notes.save')}
      </button>
    </div>
  );
}

export function SignInHistory({ accountId }: { accountId: string }) {
  const t = useT();
  const [events, setEvents] = useState<LoginEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  async function load() {
    setError(null);
    try {
      setEvents((await listLoginEvents(accountId)).events);
    } catch (cause) {
      setError(getErrorMessage(cause));
    }
  }
  async function purge() {
    setError(null);
    setConfirming(false);
    try {
      await purgeLoginEvents({ accountId });
      setEvents([]);
    } catch (cause) {
      setError(getErrorMessage(cause));
    }
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-2">
        <button className="ui-button ui-button-secondary" onClick={() => void load()}>
          {t('adminAccounts.history.show')}
        </button>
        {events && events.length > 0 && !confirming && (
          <button className="ui-button ui-button-ghost" onClick={() => setConfirming(true)}>
            {t('adminAccounts.history.purge')}
          </button>
        )}
      </div>
      {confirming && (
        <ConfirmBar
          confirm={t('adminAccounts.confirm.delete')}
          onConfirm={() => void purge()}
          onCancel={() => setConfirming(false)}
        >
          {t('adminAccounts.history.confirm')}
        </ConfirmBar>
      )}
      {error && <AccountNotice error>{error}</AccountNotice>}
      {events && (
        <ul className="space-y-1 text-xs">
          {events.length === 0 && <li className="text-muted">{t('adminAccounts.history.none')}</li>}
          {events.map((event, index) => (
            <li key={index} className="break-all">
              {new Date(event.created_at).toLocaleString()} · {event.method} · {event.outcome}
              {event.failure_reason ? ` (${event.failure_reason})` : ''} · {event.ip}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
