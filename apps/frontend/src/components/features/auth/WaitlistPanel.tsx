'use client';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import Link from 'next/link';
import { useAuth } from '@/components/providers/AuthProvider';
import { useT } from '@/components/providers/LocaleProvider';
import { getWaitlist, submitApplication, type WaitlistEntry } from '@/lib/api/accounts';
import { getErrorMessage } from '@/lib/utils/errors';
import { AccountField, AccountNotice } from './AccountFrame';

export function WaitlistPanel() {
  const { state, refreshUser, logout } = useAuth();
  const t = useT();
  const [entry, setEntry] = useState<WaitlistEntry | null>(state.user?.waitlist ?? null);
  const [purpose, setPurpose] = useState(state.user?.waitlist?.purpose ?? '');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const readEpoch = useRef(0);
  const draftEdited = useRef(false);
  useEffect(() => {
    const controller = new AbortController();
    const epoch = ++readEpoch.current;
    void getWaitlist(controller.signal)
      .then((value) => {
        if (controller.signal.aborted || epoch !== readEpoch.current) return;
        setEntry(value);
        if (!draftEdited.current) setPurpose(value.purpose);
      })
      .catch((cause) => {
        if (!controller.signal.aborted && epoch === readEpoch.current)
          setError(getErrorMessage(cause));
      });
    return () => controller.abort();
  }, [state.user?.id, state.user?.waitlist?.version]);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current || !entry || entry.status === 'pending' || entry.status === 'approved')
      return;
    ++readEpoch.current;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      setEntry(await submitApplication(purpose.trim()));
      await refreshUser();
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  async function refresh() {
    if (submitting.current) return;
    ++readEpoch.current;
    submitting.current = true;
    setBusy(true);
    setError(null);
    try {
      const current = await getWaitlist();
      setEntry(current);
      if ((current.status === 'approved') !== (state.user?.agent_access?.allowed === true))
        await refreshUser();
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      submitting.current = false;
      setBusy(false);
    }
  }
  return (
    <div className="mx-auto w-full max-w-lg space-y-5 p-6">
      <div>
        <p className="break-all text-sm text-muted">{state.user?.email}</p>
        <h1 className="mt-2 text-2xl font-semibold">{t('waitlist.title')}</h1>
        <p className="mt-2 text-sm leading-6 text-muted">{t('waitlist.description')}</p>
      </div>
      {error && <AccountNotice error>{error}</AccountNotice>}
      {entry ? (
        <>
          <div className="rounded-xl border border-border bg-surface p-4" role="status">
            <h2 className="font-medium">{t(`waitlist.status.${entry.status}`)}</h2>
            <p className="mt-2 text-sm leading-6 text-muted">
              {t(`waitlist.body.${entry.status}`)}
            </p>
            {entry.review_reason && (
              <p className="mt-3 whitespace-pre-wrap break-words text-sm">{entry.review_reason}</p>
            )}
          </div>
          {entry.status === 'approved' ? (
            <Link href="/" className="ui-button ui-button-primary">
              {t('waitlist.openAgent')}
            </Link>
          ) : entry.status === 'pending' ? (
            <p className="whitespace-pre-wrap break-words text-sm text-muted">{entry.purpose}</p>
          ) : (
            <form onSubmit={submit} className="space-y-4">
              <AccountField
                id="purpose"
                label={t('waitlist.purpose')}
                hint={t('waitlist.purposeHint')}
              >
                <textarea
                  id="purpose"
                  className="ui-input min-h-28 w-full resize-y"
                  maxLength={2000}
                  rows={4}
                  aria-describedby="purpose-hint"
                  value={purpose}
                  onChange={(e) => {
                    draftEdited.current = true;
                    setPurpose(e.target.value);
                  }}
                />
              </AccountField>
              <button className="ui-button ui-button-primary" disabled={busy} aria-busy={busy}>
                {busy
                  ? t('account.saving')
                  : entry.status === 'not_applied'
                    ? t('waitlist.apply')
                    : t('waitlist.reapply')}
              </button>
            </form>
          )}
        </>
      ) : (
        <p role="status">{error ? t('waitlist.loadFailed') : t('common.loading')}</p>
      )}
      <div className="flex flex-wrap gap-3">
        <button
          className="ui-button ui-button-secondary"
          disabled={busy}
          onClick={() => void refresh()}
        >
          {t('waitlist.refresh')}
        </button>
        <Link className="ui-button ui-button-ghost" href="/account">
          {t('security.title')}
        </Link>
        <button
          className="ui-button ui-button-ghost"
          disabled={busy}
          onClick={() => void logout().catch((cause) => setError(getErrorMessage(cause)))}
        >
          {t('account.signout')}
        </button>
      </div>
    </div>
  );
}
