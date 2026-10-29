'use client';
// Mail to accounts: compose, then follow each message and its recipients.
import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { useAuth } from '@/components/providers/AuthProvider';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import {
  cancelMail,
  listMail,
  mailDetail,
  type MailEntry,
  type MailRecipient,
} from '@/lib/api/accountAdmin';
import { getErrorMessage } from '@/lib/utils/errors';
import { AdminNavigation } from '../../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../../AdminPageHeader';
import { MailComposer } from './MailComposer';

function Recipients({ id }: { id: string }) {
  const [rows, setRows] = useState<MailRecipient[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    void mailDetail(id)
      .then((detail) => setRows(detail.recipients))
      .catch((cause) => setError(getErrorMessage(cause)));
  }, [id]);
  if (error) return <AccountNotice error>{error}</AccountNotice>;
  if (!rows) return <p className="text-xs text-muted">Loading recipients…</p>;
  return (
    <ul className="space-y-1 text-xs">
      {rows.map((row) => (
        <li key={row.account_id} className="break-all">
          {row.email} · {row.status}
          {row.error ? ` (${row.error})` : ''}
        </li>
      ))}
    </ul>
  );
}

function MailList({ reload }: { reload: number }) {
  const [entries, setEntries] = useState<MailEntry[] | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const load = useCallback(async () => {
    try {
      setEntries((await listMail()).broadcasts);
    } catch (cause) {
      setError(getErrorMessage(cause));
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load, reload, tick]);
  return (
    <section className="space-y-3 rounded-xl border border-border bg-surface p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">Messages</h2>
        <button className="ui-button ui-button-secondary" onClick={() => setTick((v) => v + 1)}>
          Refresh
        </button>
      </div>
      {error && <AccountNotice error>{error}</AccountNotice>}
      {entries?.length === 0 && <p className="text-sm text-muted">No messages yet.</p>}
      <ul className="space-y-2">
        {entries?.map((entry) => (
          <li key={entry.id} className="rounded-lg border border-border p-3 text-sm">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="break-all font-medium">{entry.subject}</span>
              <span className="text-xs text-muted">
                {entry.status} · {entry.recipient_count} recipients ·{' '}
                {entry.scheduled_at
                  ? `for ${new Date(entry.scheduled_at).toLocaleString()}`
                  : new Date(entry.created_at).toLocaleString()}
              </span>
            </div>
            <div className="mt-2 flex gap-2">
              <button
                className="ui-button ui-button-ghost"
                onClick={() => setOpen(open === entry.id ? null : entry.id)}
              >
                {open === entry.id ? 'Hide recipients' : 'Recipients'}
              </button>
              {entry.status === 'scheduled' && (
                <button
                  className="ui-button ui-button-ghost"
                  onClick={() =>
                    void cancelMail(entry.id)
                      .then(() => setTick((v) => v + 1))
                      .catch((cause) => setError(getErrorMessage(cause)))
                  }
                >
                  Cancel
                </button>
              )}
            </div>
            {open === entry.id && <Recipients id={entry.id} />}
          </li>
        ))}
      </ul>
    </section>
  );
}

export default function AccountMailPage() {
  const { state } = useAuth();
  const local = state.user?.auth_mode === 'local';
  const [reload, setReload] = useState(0);
  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section="Accounts"
          description="Mail accounts by their state: now or at a time you choose."
        />
        <AdminNavigation current="accounts" />
        <div className="mt-5 space-y-4">
          <Link className="text-sm text-accent underline" href="/admin/accounts">
            Back to accounts
          </Link>
          {local ? (
            <>
              <MailComposer onSent={() => setReload((v) => v + 1)} />
              <MailList reload={reload} />
            </>
          ) : (
            <p className="text-sm text-muted">
              Independent accounts are not enabled on this deployment.
            </p>
          )}
        </div>
      </section>
    </div>
  );
}
