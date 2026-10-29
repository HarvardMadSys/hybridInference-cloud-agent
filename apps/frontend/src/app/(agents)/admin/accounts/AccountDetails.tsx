'use client';
// What an administrator keeps about an account beside its decisions: a private
// note, the message a suspended account is shown at sign-in, and its recent
// sign-in attempts (which can be purged), as HybridInference's user detail.
import { useState } from 'react';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import type { AccountEntry } from '@/lib/api/accounts';
import {
  listLoginEvents,
  purgeLoginEvents,
  saveAccountNotes,
  type LoginEvent,
} from '@/lib/api/accountAdmin';
import { getErrorMessage } from '@/lib/utils/errors';

export function AccountNotes({ account, onSaved }: { account: AccountEntry; onSaved: () => void }) {
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
        Administrator note (never shown to the account)
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
        Message shown when a suspended account signs in
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
        Save notes
      </button>
    </div>
  );
}

export function SignInHistory({ accountId }: { accountId: string }) {
  const [events, setEvents] = useState<LoginEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
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
          Show sign-in history
        </button>
        {events && events.length > 0 && (
          <button className="ui-button ui-button-ghost" onClick={() => void purge()}>
            Purge this account’s history
          </button>
        )}
      </div>
      {error && <AccountNotice error>{error}</AccountNotice>}
      {events && (
        <ul className="space-y-1 text-xs">
          {events.length === 0 && <li className="text-muted">No sign-in attempts recorded.</li>}
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
