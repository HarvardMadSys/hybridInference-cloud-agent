'use client';
// Composing mail to accounts, adapted from HybridInference's broadcast email
// tab: a ready-made message or a subject with a Markdown body, the account
// states and application states it goes to, a preview with the recipient
// count, a test to yourself, and sending now or at a chosen time. As upstream,
// a preview belongs to the exact message and audience it showed: any change
// asks for a new one, and sending is confirmed with the recipient count.
import { useState } from 'react';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import { useT } from '@/components/providers/LocaleProvider';
import type { WaitlistStatus } from '@/lib/api/accounts';
import {
  createMail,
  previewMail,
  testMail,
  type AccountStatus,
  type MailMessage,
  type MailPreview,
} from '@/lib/api/accountAdmin';
import { getErrorMessage } from '@/lib/utils/errors';

// A deleted account is never mailed, so it is no audience.
const STATUSES: AccountStatus[] = ['active', 'suspended'];
const ADMISSIONS: WaitlistStatus[] = ['approved', 'pending', 'not_applied', 'rejected', 'revoked'];
const TEMPLATE_FIELDS: Record<'maintenance' | 'announcement', string[]> = {
  maintenance: ['date', 'duration'],
  announcement: ['feature_name', 'description'],
};

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((item) => item !== value) : [...list, value];
}

function Choices<T extends string>({
  legend,
  all,
  chosen,
  onChange,
}: {
  legend: string;
  all: T[];
  chosen: T[];
  onChange: (value: T[]) => void;
}) {
  return (
    <fieldset className="flex flex-wrap gap-3 text-sm">
      <legend className="mb-1 w-full font-medium">{legend}</legend>
      {all.map((value) => (
        <label key={value} className="flex items-center gap-1">
          <input
            type="checkbox"
            checked={chosen.includes(value)}
            onChange={() => onChange(toggle(chosen, value))}
          />
          {value.replaceAll('_', ' ')}
        </label>
      ))}
    </fieldset>
  );
}

function MessageFields({
  message,
  onMessage,
}: {
  message: MailMessage;
  onMessage: (change: Partial<MailMessage>) => void;
}) {
  const t = useT();
  const template = message.template_key;
  return (
    <>
      <label className="block text-sm" htmlFor="mail-template">
        {t('adminAccounts.mail.message')}
      </label>
      <select
        id="mail-template"
        className="ui-input"
        value={template ?? ''}
        onChange={(e) =>
          onMessage({
            template_key: (e.target.value || null) as MailMessage['template_key'],
            template_vars: {},
          })
        }
      >
        <option value="">{t('adminAccounts.mail.custom')}</option>
        <option value="maintenance">{t('adminAccounts.mail.maintenance')}</option>
        <option value="announcement">{t('adminAccounts.mail.announcement')}</option>
      </select>
      {template ? (
        TEMPLATE_FIELDS[template].map((field) => (
          <input
            key={field}
            className="ui-input w-full"
            aria-label={field.replaceAll('_', ' ')}
            placeholder={field.replaceAll('_', ' ')}
            value={message.template_vars[field] ?? ''}
            onChange={(e) =>
              onMessage({ template_vars: { ...message.template_vars, [field]: e.target.value } })
            }
          />
        ))
      ) : (
        <>
          <input
            className="ui-input w-full"
            aria-label={t('adminAccounts.mail.subject')}
            placeholder={t('adminAccounts.mail.subject')}
            maxLength={300}
            value={message.subject}
            onChange={(e) => onMessage({ subject: e.target.value })}
          />
          <textarea
            className="ui-input w-full font-mono"
            aria-label={t('adminAccounts.mail.body')}
            placeholder={t('adminAccounts.mail.bodyPlaceholder')}
            rows={8}
            maxLength={100000}
            value={message.body_markdown}
            onChange={(e) => onMessage({ body_markdown: e.target.value })}
          />
        </>
      )}
      <Choices
        legend={t('adminAccounts.mail.accountState')}
        all={STATUSES}
        chosen={message.target_statuses}
        onChange={(target_statuses) => onMessage({ target_statuses })}
      />
      <Choices
        legend={t('adminAccounts.mail.applicationState')}
        all={ADMISSIONS}
        chosen={message.target_admissions}
        onChange={(target_admissions) => onMessage({ target_admissions })}
      />
    </>
  );
}

function SendConfirmation({
  count,
  scheduledAt,
  busy,
  onConfirm,
  onBack,
}: {
  count: number;
  scheduledAt: Date | null;
  busy: boolean;
  onConfirm: () => void;
  onBack: () => void;
}) {
  const t = useT();
  const when = scheduledAt?.toLocaleString();
  const question = when
    ? t('adminAccounts.mail.confirmScheduleText', { count, when })
    : t('adminAccounts.mail.confirmSendText', { count });
  return (
    <div className="space-y-2 rounded-lg border border-border p-3" role="alertdialog">
      <p className="text-sm">{question}</p>
      <div className="flex gap-2">
        <button className="ui-button ui-button-primary" disabled={busy} onClick={onConfirm}>
          {scheduledAt
            ? t('adminAccounts.mail.confirmSchedule')
            : t('adminAccounts.mail.confirmSend')}
        </button>
        <button className="ui-button ui-button-secondary" disabled={busy} onClick={onBack}>
          {t('adminAccounts.mail.back')}
        </button>
      </div>
    </div>
  );
}

function PreviewPanel({ preview }: { preview: MailPreview }) {
  const t = useT();
  return (
    <div className="space-y-2 rounded-lg border border-border p-3">
      <p className="text-sm">
        {t('adminAccounts.mail.recipients', {
          count: preview.recipient_count,
          subject: preview.subject,
        })}
      </p>
      {preview.delivery_problem && <AccountNotice error>{preview.delivery_problem}</AccountNotice>}
      <iframe
        title={t('adminAccounts.mail.previewFrame')}
        sandbox=""
        className="h-64 w-full rounded border border-border bg-surface"
        srcDoc={preview.html}
      />
    </div>
  );
}

function ComposerActions({
  busy,
  ready,
  sendable,
  scheduled,
  onPreview,
  onTest,
  onSend,
}: {
  busy: boolean;
  ready: boolean;
  sendable: boolean;
  scheduled: boolean;
  onPreview: () => void;
  onTest: () => void;
  onSend: () => void;
}) {
  const t = useT();
  return (
    <div className="flex flex-wrap gap-2">
      <button
        className="ui-button ui-button-secondary"
        disabled={busy || !ready}
        onClick={onPreview}
      >
        {t('adminAccounts.mail.preview')}
      </button>
      <button className="ui-button ui-button-secondary" disabled={busy || !ready} onClick={onTest}>
        {t('adminAccounts.mail.test')}
      </button>
      <button className="ui-button ui-button-primary" disabled={busy || !sendable} onClick={onSend}>
        {scheduled ? t('adminAccounts.mail.schedule') : t('adminAccounts.mail.send')}
      </button>
    </div>
  );
}

const EMPTY: MailMessage = {
  template_key: null,
  template_vars: {},
  subject: '',
  body_markdown: '',
  target_statuses: ['active'],
  target_admissions: ['approved'],
};

export function MailComposer({ onSent }: { onSent: () => void }) {
  const t = useT();
  const [message, setMessage] = useState<MailMessage>(EMPTY);
  const [when, setWhen] = useState('');
  // A preview, and the exact message and audience it showed.
  const [shown, setShown] = useState<{ preview: MailPreview; key: string } | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function run(action: () => Promise<string | null>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      setNotice(await action());
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }
  const ready = message.target_statuses.length > 0 && message.target_admissions.length > 0;
  const key = JSON.stringify(message);
  const current = shown?.key === key ? shown.preview : null;
  const sendable = ready && current !== null && !current.delivery_problem;
  const scheduledAt = when ? new Date(when) : null;
  return (
    <section className="space-y-3 rounded-xl border border-border bg-surface p-4">
      <h2 className="text-base font-semibold">{t('adminAccounts.mail.new')}</h2>
      <MessageFields
        message={message}
        onMessage={(change) => setMessage((previous) => ({ ...previous, ...change }))}
      />
      <label className="block text-sm" htmlFor="mail-when">
        {t('adminAccounts.mail.when')}
      </label>
      <input
        id="mail-when"
        className="ui-input"
        type="datetime-local"
        value={when}
        onChange={(e) => setWhen(e.target.value)}
      />
      {error && <AccountNotice error>{error}</AccountNotice>}
      {notice && <AccountNotice>{notice}</AccountNotice>}
      <ComposerActions
        busy={busy}
        ready={ready}
        sendable={sendable && !confirming}
        scheduled={Boolean(when)}
        onPreview={() =>
          void run(async () => {
            setShown({ preview: await previewMail(message), key });
            setConfirming(false);
            return null;
          })
        }
        onTest={() =>
          void run(async () => {
            await testMail(message);
            return t('adminAccounts.mail.testSent');
          })
        }
        onSend={() => setConfirming(true)}
      />
      {shown && !current && <AccountNotice>{t('adminAccounts.mail.stale')}</AccountNotice>}
      {current && confirming && (
        <SendConfirmation
          count={current.recipient_count}
          scheduledAt={scheduledAt}
          busy={busy}
          onBack={() => setConfirming(false)}
          onConfirm={() =>
            void run(async () => {
              const created = await createMail(message, scheduledAt?.toISOString() ?? null);
              setConfirming(false);
              setShown(null);
              onSent();
              return t('adminAccounts.mail.queued', { count: created.recipient_count });
            })
          }
        />
      )}
      {current && <PreviewPanel preview={current} />}
    </section>
  );
}
