'use client';
// Mail to accounts: compose, then follow each message and its recipients, a
// page at a time. Cancelling a scheduled message is confirmed first.
import Link from 'next/link';
import { useCallback, useEffect, useState } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import {
  MAIL_PAGE,
  RECIPIENT_PAGE,
  cancelMail,
  listMail,
  mailDetail,
  type MailEntry,
  type MailRecipient,
} from '@/lib/api/accountAdmin';
import type { Translator } from '@/lib/i18n/translate';
import { getErrorMessage } from '@/lib/utils/errors';
import { AdminNavigation } from '../../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../../AdminPageHeader';
import { MailComposer } from './MailComposer';

function Pager({
  offset,
  size,
  total,
  onOffset,
}: {
  offset: number;
  size: number;
  total: number;
  onOffset: (offset: number) => void;
}) {
  const t = useT();
  if (total <= size) return null;
  return (
    <div className="flex items-center gap-2 text-xs">
      <button
        className="ui-button ui-button-ghost"
        disabled={offset === 0}
        onClick={() => onOffset(Math.max(0, offset - size))}
      >
        {t('adminAccounts.previous')}
      </button>
      <span className="text-muted">
        {t('adminAccounts.mail.page', {
          from: offset + 1,
          to: Math.min(offset + size, total),
          total,
        })}
      </span>
      <button
        className="ui-button ui-button-ghost"
        disabled={offset + size >= total}
        onClick={() => onOffset(offset + size)}
      >
        {t('adminAccounts.next')}
      </button>
    </div>
  );
}

function Recipients({ id }: { id: string }) {
  const t = useT();
  const [rows, setRows] = useState<MailRecipient[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    void mailDetail(id, offset, controller.signal)
      .then((detail) => {
        setRows(detail.recipients);
        setTotal(detail.total_recipients);
      })
      .catch((cause) => {
        if (!controller.signal.aborted) setError(getErrorMessage(cause));
      });
    return () => controller.abort();
  }, [id, offset]);
  if (error) return <AccountNotice error>{error}</AccountNotice>;
  if (!rows)
    return <p className="text-xs text-muted">{t('adminAccounts.mail.loadingRecipients')}</p>;
  return (
    <div className="space-y-2">
      <ul className="space-y-1 text-xs">
        {rows.map((row) => (
          <li key={row.account_id} className="break-all">
            {row.email} · {row.status}
            {row.error ? ` (${row.error})` : ''}
          </li>
        ))}
      </ul>
      <Pager offset={offset} size={RECIPIENT_PAGE} total={total} onOffset={setOffset} />
    </div>
  );
}

function entryTime(entry: MailEntry, t: Translator): string {
  if (!entry.scheduled_at) return new Date(entry.created_at).toLocaleString();
  return t('adminAccounts.mail.for', { when: new Date(entry.scheduled_at).toLocaleString() });
}

function MailList({ reload }: { reload: number }) {
  const t = useT();
  const [entries, setEntries] = useState<MailEntry[] | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const load = useCallback(async () => {
    try {
      const page = await listMail(offset);
      setEntries(page.broadcasts);
      setTotal(page.total);
    } catch (cause) {
      setError(getErrorMessage(cause));
    }
  }, [offset]);
  useEffect(() => {
    void load();
  }, [load, reload, tick]);
  return (
    <section className="space-y-3 rounded-xl border border-border bg-surface p-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('adminAccounts.mail.messages')}</h2>
        <button className="ui-button ui-button-secondary" onClick={() => setTick((v) => v + 1)}>
          {t('adminAccounts.refresh')}
        </button>
      </div>
      {error && <AccountNotice error>{error}</AccountNotice>}
      {entries?.length === 0 && (
        <p className="text-sm text-muted">{t('adminAccounts.mail.none')}</p>
      )}
      <ul className="space-y-2">
        {entries?.map((entry) => (
          <li key={entry.id} className="rounded-lg border border-border p-3 text-sm">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="break-all font-medium">{entry.subject}</span>
              <span className="text-xs text-muted">
                {t('adminAccounts.mail.entry', {
                  status: entry.status,
                  count: entry.recipient_count,
                  when: entryTime(entry, t),
                })}
              </span>
            </div>
            <div className="mt-2 flex gap-2">
              <button
                className="ui-button ui-button-ghost"
                onClick={() => setOpen(open === entry.id ? null : entry.id)}
              >
                {open === entry.id
                  ? t('adminAccounts.mail.hideRecipients')
                  : t('adminAccounts.mail.showRecipients')}
              </button>
              {entry.status === 'scheduled' && cancelling !== entry.id && (
                <button
                  className="ui-button ui-button-ghost"
                  onClick={() => setCancelling(entry.id)}
                >
                  {t('adminAccounts.mail.cancel')}
                </button>
              )}
            </div>
            {cancelling === entry.id && (
              <div className="mt-2 flex flex-wrap items-center gap-2" role="alertdialog">
                <span className="text-sm">
                  {t('adminAccounts.mail.cancelConfirm', { count: entry.recipient_count })}
                </span>
                <button
                  className="ui-button ui-button-danger"
                  onClick={() =>
                    void cancelMail(entry.id)
                      .then(() => setTick((v) => v + 1))
                      .catch((cause) => setError(getErrorMessage(cause)))
                      .finally(() => setCancelling(null))
                  }
                >
                  {t('adminAccounts.mail.cancelMessage')}
                </button>
                <button
                  className="ui-button ui-button-secondary"
                  onClick={() => setCancelling(null)}
                >
                  {t('adminAccounts.mail.keep')}
                </button>
              </div>
            )}
            {open === entry.id && <Recipients id={entry.id} />}
          </li>
        ))}
      </ul>
      <Pager offset={offset} size={MAIL_PAGE} total={total} onOffset={setOffset} />
    </section>
  );
}

export default function AccountMailPage() {
  const t = useT();
  const [reload, setReload] = useState(0);
  return (
    <div className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section={t('adminAccounts.section')}
          description={t('adminAccounts.mail.description')}
        />
        <AdminNavigation current="accounts" />
        <div className="mt-5 space-y-4">
          <Link className="text-sm text-accent underline" href="/admin/accounts">
            {t('adminAccounts.back')}
          </Link>
              <MailComposer onSent={() => setReload((v) => v + 1)} />
              <MailList reload={reload} />
        </div>
      </section>
    </div>
  );
}
