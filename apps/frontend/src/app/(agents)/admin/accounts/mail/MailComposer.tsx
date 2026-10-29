'use client';
// Composing mail to accounts, adapted from HybridInference's broadcast email
// tab: a ready-made message or a subject with a Markdown body, the account
// states and application states it goes to, a preview with the recipient
// count, a test to yourself, and sending now or at a chosen time.
import { useState } from 'react';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
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

export function MailComposer({ onSent }: { onSent: () => void }) {
  const [template, setTemplate] = useState<MailMessage['template_key']>(null);
  const [vars, setVars] = useState<Record<string, string>>({});
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const [statuses, setStatuses] = useState<AccountStatus[]>(['active']);
  const [admissions, setAdmissions] = useState<WaitlistStatus[]>(['approved']);
  const [when, setWhen] = useState('');
  const [preview, setPreview] = useState<MailPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const message: MailMessage = {
    template_key: template,
    template_vars: vars,
    subject,
    body_markdown: body,
    target_statuses: statuses,
    target_admissions: admissions,
  };
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
  const ready = statuses.length > 0 && admissions.length > 0;
  const undeliverable = preview?.delivery_problem ?? null;
  return (
    <section className="space-y-3 rounded-xl border border-border bg-surface p-4">
      <h2 className="text-base font-semibold">New message</h2>
      <label className="block text-sm" htmlFor="mail-template">
        Message
      </label>
      <select
        id="mail-template"
        className="ui-input"
        value={template ?? ''}
        onChange={(e) => {
          setTemplate((e.target.value || null) as MailMessage['template_key']);
          setVars({});
        }}
      >
        <option value="">Custom (subject and Markdown)</option>
        <option value="maintenance">Scheduled maintenance</option>
        <option value="announcement">Announcement</option>
      </select>
      {template ? (
        TEMPLATE_FIELDS[template].map((field) => (
          <input
            key={field}
            className="ui-input w-full"
            aria-label={field.replaceAll('_', ' ')}
            placeholder={field.replaceAll('_', ' ')}
            value={vars[field] ?? ''}
            onChange={(e) => setVars({ ...vars, [field]: e.target.value })}
          />
        ))
      ) : (
        <>
          <input
            className="ui-input w-full"
            aria-label="Subject"
            placeholder="Subject"
            maxLength={300}
            value={subject}
            onChange={(e) => setSubject(e.target.value)}
          />
          <textarea
            className="ui-input w-full font-mono"
            aria-label="Body (Markdown)"
            placeholder="Body (Markdown; HTML is not allowed)"
            rows={8}
            maxLength={100000}
            value={body}
            onChange={(e) => setBody(e.target.value)}
          />
        </>
      )}
      <Choices legend="Account state" all={STATUSES} chosen={statuses} onChange={setStatuses} />
      <Choices
        legend="Application state"
        all={ADMISSIONS}
        chosen={admissions}
        onChange={setAdmissions}
      />
      <label className="block text-sm" htmlFor="mail-when">
        Send at (leave empty to send now)
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
      <div className="flex flex-wrap gap-2">
        <button
          className="ui-button ui-button-secondary"
          disabled={busy || !ready}
          onClick={() =>
            void run(async () => {
              setPreview(await previewMail(message));
              return null;
            })
          }
        >
          Preview
        </button>
        <button
          className="ui-button ui-button-secondary"
          disabled={busy || !ready}
          onClick={() =>
            void run(async () => {
              await testMail(message);
              return 'A test was sent to your own verified address.';
            })
          }
        >
          Send me a test
        </button>
        <button
          className="ui-button ui-button-primary"
          disabled={busy || !ready || !preview || Boolean(undeliverable)}
          onClick={() =>
            void run(async () => {
              const created = await createMail(message, when ? new Date(when).toISOString() : null);
              onSent();
              return `Queued for ${created.recipient_count} accounts.`;
            })
          }
        >
          {when ? 'Schedule' : 'Send'}
        </button>
      </div>
      {preview && (
        <div className="space-y-2 rounded-lg border border-border p-3">
          <p className="text-sm">
            {preview.recipient_count} recipients (verified addresses only) · {preview.subject}
          </p>
          {undeliverable && <AccountNotice error>{undeliverable}</AccountNotice>}
          <iframe
            title="Message preview"
            sandbox=""
            className="h-64 w-full rounded border border-border bg-surface"
            srcDoc={preview.html}
          />
        </div>
      )}
    </section>
  );
}
