'use client';

import { useEffect, useState } from 'react';

import type { AgentRunnerHost } from '@/lib/api/admin';
import type {
  SandboxArchiveSettings,
  SandboxArchiveSettingsUpdate,
} from '@/lib/api/adminArchiveSettings';
import { SANDBOX_ARCHIVE_SETTINGS_CAPABILITY } from '@/lib/api/adminArchiveSettings';

import { SettingsSection } from '../SettingsSection';

const MIB = 1024 * 1024;
const GIB = 1024 * MIB;
const MAX_ARCHIVE_BYTES = 4 * GIB;
const MAX_SINGLE_UPLOAD_BYTES = 64 * MIB;
const MIN_MULTIPART_PART_BYTES = 5 * MIB;
const MAX_MULTIPART_PART_BYTES = 64 * MIB;
const MAX_MULTIPART_PARTS = 10_000;

const KMS_KEY_ARN = /^arn:([^:]+):kms:([a-z0-9-]+):([0-9]{12}):key\/([A-Za-z0-9-]+)$/;
const ACCESS_KEY_ID = /^[A-Z0-9]{20}$/;
const SECRET_ACCESS_KEY = /^[A-Za-z0-9/+=]{40}$/;

interface Draft {
  enabled: boolean;
  region: string;
  kmsKeyArn: string;
  expectedBucketOwner: string;
  awsAccessKeyId: string;
  awsSecretAccessKey: string;
  maxArchiveBytes: string;
  multipartThresholdBytes: string;
  multipartPartBytes: string;
}

interface EditorState {
  draft: Draft;
  saved: Draft;
  revision: number;
}

type DraftErrors = Partial<Record<keyof Draft | 'form', string>>;

function initialDraft(settings: SandboxArchiveSettings): Draft {
  return {
    enabled: settings.enabled,
    region: settings.region,
    kmsKeyArn: settings.kms_key_arn,
    expectedBucketOwner: settings.expected_bucket_owner,
    awsAccessKeyId: settings.aws_access_key_id,
    awsSecretAccessKey: '',
    maxArchiveBytes: String(settings.max_archive_bytes),
    multipartThresholdBytes: String(settings.multipart_threshold_bytes),
    multipartPartBytes: String(settings.multipart_part_bytes),
  };
}

function draftsMatch(left: Draft, right: Draft): boolean {
  return (Object.keys(left) as (keyof Draft)[]).every((key) => left[key] === right[key]);
}

function wholeNumber(
  raw: string,
  minimum: number,
  maximum: number,
  error: string,
): { value: number } | { error: string } {
  const value = Number(raw);
  return raw.trim() !== '' && Number.isSafeInteger(value) && value >= minimum && value <= maximum
    ? { value }
    : { error };
}

export function validateSandboxArchiveSettings(
  draft: Draft,
  revision: number,
  saved: { accessKeyId: string; secretConfigured: boolean } = {
    accessKeyId: '',
    secretConfigured: false,
  },
): { update: SandboxArchiveSettingsUpdate } | { errors: DraftErrors } {
  const errors: DraftErrors = {};
  const region = draft.region;
  const kmsKeyArn = draft.kmsKeyArn;
  const expectedBucketOwner = draft.expectedBucketOwner;
  const awsAccessKeyId = draft.awsAccessKeyId;
  const awsSecretAccessKey = draft.awsSecretAccessKey;
  const keepingSecret =
    saved.secretConfigured &&
    awsAccessKeyId !== '' &&
    awsAccessKeyId === saved.accessKeyId &&
    awsSecretAccessKey === '';
  const usingAccessKey =
    ACCESS_KEY_ID.test(awsAccessKeyId) &&
    (keepingSecret || SECRET_ACCESS_KEY.test(awsSecretAccessKey));

  if (draft.enabled && region === '') errors.region = 'Enter the AWS region.';
  else if (region !== '' && region.trim() !== region)
    errors.region = 'Remove leading or trailing spaces.';
  else if (region.length > 128) errors.region = 'Enter a region of at most 128 characters.';

  if (awsAccessKeyId !== '' && !ACCESS_KEY_ID.test(awsAccessKeyId)) {
    errors.awsAccessKeyId = 'Enter a 20-character AWS access key ID.';
  }
  if ((awsAccessKeyId !== '' || awsSecretAccessKey !== '') && !keepingSecret) {
    if (!SECRET_ACCESS_KEY.test(awsSecretAccessKey)) {
      errors.awsSecretAccessKey = 'Enter the AWS secret access key.';
    }
  }
  if (awsSecretAccessKey !== '' && awsAccessKeyId === '') {
    errors.awsAccessKeyId = 'Enter the AWS access key ID.';
  }

  if (draft.enabled && !usingAccessKey && expectedBucketOwner === '') {
    errors.expectedBucketOwner = 'Enter the expected bucket owner.';
  } else if (expectedBucketOwner !== '' && !/^[0-9]{12}$/.test(expectedBucketOwner)) {
    errors.expectedBucketOwner = 'Enter a 12-digit AWS account ID.';
  }

  const kmsMatch = KMS_KEY_ARN.exec(kmsKeyArn);
  if (draft.enabled && !usingAccessKey && kmsKeyArn === '') {
    errors.kmsKeyArn = 'Enter the customer-managed KMS key ARN.';
  } else if (kmsKeyArn.length > 2048) {
    errors.kmsKeyArn = 'Enter a KMS key ARN of at most 2048 characters.';
  } else if (kmsKeyArn !== '' && kmsMatch === null) {
    errors.kmsKeyArn = 'Enter a KMS key ARN ending in key/<key-id>; aliases are not accepted.';
  } else if (kmsMatch !== null) {
    if (region !== '' && kmsMatch[2] !== region) {
      errors.kmsKeyArn = 'The KMS key region must match the archive region.';
    } else if (expectedBucketOwner !== '' && kmsMatch[3] !== expectedBucketOwner) {
      errors.kmsKeyArn = 'The KMS key account must match the expected bucket owner.';
    }
  }

  const maxArchive = wholeNumber(
    draft.maxArchiveBytes,
    1,
    MAX_ARCHIVE_BYTES,
    `Enter a whole number from 1 to ${MAX_ARCHIVE_BYTES}.`,
  );
  if ('error' in maxArchive) errors.maxArchiveBytes = maxArchive.error;

  const thresholdMaximum =
    'value' in maxArchive
      ? Math.min(maxArchive.value, MAX_SINGLE_UPLOAD_BYTES)
      : MAX_SINGLE_UPLOAD_BYTES;
  const threshold = wholeNumber(
    draft.multipartThresholdBytes,
    1,
    thresholdMaximum,
    `Enter a whole number from 1 to ${thresholdMaximum}.`,
  );
  if ('error' in threshold) errors.multipartThresholdBytes = threshold.error;

  const part = wholeNumber(
    draft.multipartPartBytes,
    MIN_MULTIPART_PART_BYTES,
    MAX_MULTIPART_PART_BYTES,
    `Enter a whole number from ${MIN_MULTIPART_PART_BYTES} to ${MAX_MULTIPART_PART_BYTES}.`,
  );
  if ('error' in part) errors.multipartPartBytes = part.error;

  if (
    'value' in maxArchive &&
    'value' in part &&
    Math.ceil(maxArchive.value / part.value) > MAX_MULTIPART_PARTS
  ) {
    errors.form = `Choose a larger multipart part size so an archive uses at most ${MAX_MULTIPART_PARTS} parts.`;
  }

  if (Object.keys(errors).length > 0) return { errors };
  return {
    update: {
      enabled: draft.enabled,
      region,
      kms_key_arn: kmsKeyArn,
      expected_bucket_owner: expectedBucketOwner,
      aws_access_key_id: awsAccessKeyId,
      aws_secret_access_key: awsSecretAccessKey,
      max_archive_bytes: (maxArchive as { value: number }).value,
      multipart_threshold_bytes: (threshold as { value: number }).value,
      multipart_part_bytes: (part as { value: number }).value,
      revision,
    },
  };
}

function formatBytes(value: string): string {
  const amount = Number(value);
  if (!Number.isSafeInteger(amount) || amount < 1) return '';
  if (amount % GIB === 0) return `${amount / GIB} GiB`;
  if (amount % MIB === 0) return `${amount / MIB} MiB`;
  return `${amount.toLocaleString('en-US')} bytes`;
}

const INPUT =
  'mt-1 w-full rounded-md border border-gray-200 bg-white px-3 py-2 text-sm text-gray-800 shadow-sm disabled:opacity-50 read-only:bg-gray-50 aria-[invalid=true]:border-red-400 dark:border-white/10 dark:bg-gray-950 dark:text-gray-100 dark:read-only:bg-gray-950/50';

function ErrorText({ id, error }: { id: string; error: string | undefined }) {
  return error ? (
    <p id={id} role="alert" className="mt-1 text-xs text-red-600 dark:text-red-400">
      {error}
    </p>
  ) : null;
}

export default function SandboxArchiveSettingsCard({
  settings,
  hosts = [],
  busy,
  onSave,
}: {
  settings: SandboxArchiveSettings;
  hosts?: AgentRunnerHost[];
  busy: boolean;
  onSave: (update: SandboxArchiveSettingsUpdate) => void;
}) {
  const [editor, setEditor] = useState<EditorState>(() => {
    const saved = initialDraft(settings);
    return { draft: saved, saved, revision: settings.revision };
  });
  const [errors, setErrors] = useState<DraftErrors>({});
  const { draft } = editor;
  const dirty = !draftsMatch(draft, editor.saved);
  const stale = dirty && editor.revision !== settings.revision;

  useEffect(() => {
    setEditor((current) => {
      if (current.revision === settings.revision || !draftsMatch(current.draft, current.saved)) {
        return current;
      }
      const saved = initialDraft(settings);
      return { draft: saved, saved, revision: settings.revision };
    });
  }, [settings]);

  const set = <Key extends keyof Draft>(key: Key, value: Draft[Key]) => {
    setEditor((current) => ({
      ...current,
      draft: { ...current.draft, [key]: value },
    }));
    setErrors((current) => ({ ...current, [key]: undefined, form: undefined }));
  };
  const loadLatest = () => {
    const saved = initialDraft(settings);
    setEditor({ draft: saved, saved, revision: settings.revision });
    setErrors({});
  };

  const numericFields = [
    {
      key: 'maxArchiveBytes' as const,
      targetId: 'archive-setting-max_archive_bytes',
      label: 'Maximum archive bytes',
      description: 'Maximum compressed size of one archive. Hard limit: 4 GiB.',
    },
    {
      key: 'multipartThresholdBytes' as const,
      targetId: 'archive-setting-multipart_threshold_bytes',
      label: 'Multipart threshold bytes',
      description: 'Archives at or above this size use multipart upload. Maximum: 64 MiB.',
    },
    {
      key: 'multipartPartBytes' as const,
      targetId: 'archive-setting-multipart_part_bytes',
      label: 'Multipart part bytes',
      description: 'Size of each uploaded part, from 5 MiB through 64 MiB.',
    },
  ];
  const supportedHosts = hosts.filter((host) =>
    host.capabilities.includes(SANDBOX_ARCHIVE_SETTINGS_CAPABILITY),
  );
  const appliedHosts = supportedHosts.filter(
    (host) => host.sandbox_archive_settings_applied_revision === settings.revision,
  );
  const pendingHosts = supportedHosts.filter(
    (host) => host.sandbox_archive_settings_applied_revision !== settings.revision,
  );
  const unsupportedHosts = hosts.filter(
    (host) => !host.capabilities.includes(SANDBOX_ARCHIVE_SETTINGS_CAPABILITY),
  );
  const compatibleNoun = supportedHosts.length === 1 ? 'server' : 'servers';
  const unsupportedVerb = unsupportedHosts.length === 1 ? 'needs' : 'need';
  const rolloutText =
    hosts.length === 0
      ? 'No active server is available to apply this revision.'
      : `Applied on ${appliedHosts.length} of ${supportedHosts.length} compatible ${compatibleNoun}.`;
  const pendingText =
    pendingHosts.length > 0
      ? ` Waiting for ${pendingHosts.map((host) => host.host).join(', ')}.`
      : '';
  const unsupportedText =
    unsupportedHosts.length > 0
      ? ` ${unsupportedHosts.map((host) => host.host).join(', ')} ${unsupportedVerb} a redeploy.`
      : '';

  return (
    <section
      aria-labelledby="sandbox-archive-settings-title"
      className="mt-8 rounded-2xl border border-gray-200 bg-white p-5 dark:border-white/10 dark:bg-gray-900"
    >
      <h2
        id="sandbox-archive-settings-title"
        className="text-base font-semibold text-gray-900 dark:text-gray-50"
      >
        Sandbox archives
      </h2>
      <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
        Save the initial workspace and each broker-managed clean-stop state. An AWS access key is
        stored encrypted for the workspace broker. Leave it blank to use each broker&apos;s workload
        identity.
      </p>
      <p className="mt-1 text-[11px] text-gray-500 dark:text-gray-400">
        {rolloutText}
        {pendingText}
        {unsupportedText}
      </p>
      <form
        noValidate
        className="mt-5 space-y-5"
        onSubmit={(event) => {
          event.preventDefault();
          const result = validateSandboxArchiveSettings(draft, editor.revision, {
            accessKeyId: editor.saved.awsAccessKeyId,
            secretConfigured: settings.aws_secret_access_key_configured,
          });
          if ('errors' in result) {
            setErrors(result.errors);
            return;
          }
          setErrors({});
          onSave(result.update);
        }}
      >
        {stale && (
          <div
            role="alert"
            className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-800 dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-300"
          >
            These settings changed while you were editing. Your draft is preserved, but it cannot be
            saved over the newer revision.{' '}
            <button type="button" onClick={loadLatest} className="font-medium underline">
              Discard the draft and load the latest settings
            </button>
            .
          </div>
        )}

        <SettingsSection
          title="Important"
          description="Enable archives and configure their destination, ownership, and encryption."
        >
          <div className="space-y-4" aria-labelledby="archive-storage-heading">
            <h3
              id="archive-storage-heading"
              className="text-sm font-semibold text-gray-900 dark:text-gray-50"
            >
              Archive storage
            </h3>
            <label className="flex items-start gap-3 text-sm text-gray-800 dark:text-gray-200">
              <input
                id="archive-setting-enabled"
                type="checkbox"
                aria-label="Enable sandbox archives"
                aria-describedby="archive-enabled-description"
                checked={draft.enabled}
                disabled={busy}
                onChange={(event) => set('enabled', event.target.checked)}
                className="mt-0.5 h-4 w-4 rounded border-gray-300 text-crimson focus:ring-crimson dark:border-white/20 dark:bg-gray-950"
              />
              <span>
                <span className="block font-medium">Enable sandbox archives</span>
                <span
                  id="archive-enabled-description"
                  className="mt-0.5 block text-xs text-gray-500 dark:text-gray-400"
                >
                  With an access key, uploads use that key. Without one, brokers use their workload
                  identity and need the bucket owner and KMS key. Bucket versioning is required
                  either way.
                </span>
              </span>
            </label>

            <div className="grid gap-4 sm:grid-cols-2">
              <label className="sm:col-span-2 text-sm font-medium text-gray-800 dark:text-gray-200">
                S3 destination
                <input
                  id="archive-setting-s3_uri"
                  type="text"
                  value={settings.s3_uri}
                  readOnly
                  aria-label="S3 destination"
                  aria-describedby="archive-destination-description"
                  className={INPUT}
                />
                <span
                  id="archive-destination-description"
                  className="mt-1 block text-xs font-normal text-gray-500 dark:text-gray-400"
                >
                  Fixed by the deployment security policy and not included in update requests.
                </span>
              </label>

              <label className="text-sm font-medium text-gray-800 dark:text-gray-200">
                AWS region
                <input
                  id="archive-setting-region"
                  type="text"
                  value={draft.region}
                  disabled={busy}
                  aria-label="AWS region"
                  aria-invalid={errors.region !== undefined}
                  aria-describedby={errors.region ? 'archive-region-error' : undefined}
                  onChange={(event) => set('region', event.target.value)}
                  placeholder="us-east-1"
                  className={INPUT}
                />
                <ErrorText id="archive-region-error" error={errors.region} />
              </label>

              <label className="text-sm font-medium text-gray-800 dark:text-gray-200">
                AWS access key ID
                <input
                  id="archive-setting-aws_access_key_id"
                  type="text"
                  value={draft.awsAccessKeyId}
                  disabled={busy}
                  autoComplete="off"
                  spellCheck={false}
                  aria-label="AWS access key ID"
                  aria-invalid={errors.awsAccessKeyId !== undefined}
                  aria-describedby={
                    errors.awsAccessKeyId ? 'archive-access-key-error' : 'archive-access-key-hint'
                  }
                  onChange={(event) => set('awsAccessKeyId', event.target.value)}
                  placeholder="AKIAIOSFODNN7EXAMPLE"
                  className={INPUT}
                />
                <span
                  id="archive-access-key-hint"
                  className="mt-1 block text-xs font-normal text-gray-500 dark:text-gray-400"
                >
                  Use this instead of the bucket owner and KMS key.
                </span>
                <ErrorText id="archive-access-key-error" error={errors.awsAccessKeyId} />
              </label>

              <label className="text-sm font-medium text-gray-800 dark:text-gray-200">
                AWS secret access key
                <input
                  id="archive-setting-aws_secret_access_key"
                  type="password"
                  value={draft.awsSecretAccessKey}
                  disabled={busy}
                  autoComplete="new-password"
                  spellCheck={false}
                  aria-label="AWS secret access key"
                  aria-invalid={errors.awsSecretAccessKey !== undefined}
                  aria-describedby={
                    errors.awsSecretAccessKey
                      ? 'archive-secret-key-error'
                      : 'archive-secret-key-hint'
                  }
                  onChange={(event) => set('awsSecretAccessKey', event.target.value)}
                  placeholder={
                    settings.aws_secret_access_key_configured
                      ? 'Leave blank to keep the saved secret'
                      : 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
                  }
                  className={INPUT}
                />
                <span
                  id="archive-secret-key-hint"
                  className="mt-1 block text-xs font-normal text-gray-500 dark:text-gray-400"
                >
                  Write-only. It is not shown again after saving.
                </span>
                <ErrorText id="archive-secret-key-error" error={errors.awsSecretAccessKey} />
              </label>

              <label className="text-sm font-medium text-gray-800 dark:text-gray-200">
                Expected bucket owner
                <input
                  id="archive-setting-expected_bucket_owner"
                  type="text"
                  inputMode="numeric"
                  value={draft.expectedBucketOwner}
                  disabled={busy}
                  aria-label="Expected bucket owner"
                  aria-invalid={errors.expectedBucketOwner !== undefined}
                  aria-describedby={
                    errors.expectedBucketOwner ? 'archive-bucket-owner-error' : undefined
                  }
                  onChange={(event) => set('expectedBucketOwner', event.target.value)}
                  placeholder="123456789012"
                  className={INPUT}
                />
                <ErrorText id="archive-bucket-owner-error" error={errors.expectedBucketOwner} />
              </label>

              <label className="sm:col-span-2 text-sm font-medium text-gray-800 dark:text-gray-200">
                Customer-managed KMS key ARN
                <input
                  id="archive-setting-kms_key_arn"
                  type="text"
                  value={draft.kmsKeyArn}
                  disabled={busy}
                  aria-label="Customer-managed KMS key ARN"
                  aria-invalid={errors.kmsKeyArn !== undefined}
                  aria-describedby={errors.kmsKeyArn ? 'archive-kms-error' : undefined}
                  onChange={(event) => set('kmsKeyArn', event.target.value)}
                  placeholder="arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000000"
                  className={INPUT}
                />
                <ErrorText id="archive-kms-error" error={errors.kmsKeyArn} />
              </label>
            </div>
          </div>
        </SettingsSection>

        <SettingsSection
          title="Advanced"
          description="Tune archive size limits and multipart uploads."
          open={Boolean(
            errors.maxArchiveBytes ||
            errors.multipartThresholdBytes ||
            errors.multipartPartBytes ||
            errors.form,
          )}
        >
          <div className="space-y-4" aria-labelledby="archive-upload-heading">
            <h3
              id="archive-upload-heading"
              className="text-sm font-semibold text-gray-900 dark:text-gray-50"
            >
              Upload limits
            </h3>
            <div className="grid gap-4 sm:grid-cols-2">
              {numericFields.map(({ key, targetId, label, description }) => (
                <label key={key} className="text-sm font-medium text-gray-800 dark:text-gray-200">
                  {label}
                  <input
                    id={targetId}
                    type="number"
                    inputMode="numeric"
                    step="1"
                    value={draft[key]}
                    disabled={busy}
                    aria-label={label}
                    aria-invalid={errors[key] !== undefined}
                    aria-describedby={`archive-${key}-description${
                      errors[key] ? ` archive-${key}-error` : ''
                    }`}
                    onChange={(event) => set(key, event.target.value)}
                    className={INPUT}
                  />
                  <span
                    id={`archive-${key}-description`}
                    className="mt-1 block text-xs font-normal text-gray-500 dark:text-gray-400"
                  >
                    {description}{' '}
                    {formatBytes(draft[key]) && `Current: ${formatBytes(draft[key])}.`}
                  </span>
                  <ErrorText id={`archive-${key}-error`} error={errors[key]} />
                </label>
              ))}
            </div>

            {errors.form && (
              <p role="alert" className="text-sm text-red-600 dark:text-red-400">
                {errors.form}
              </p>
            )}
          </div>
        </SettingsSection>

        <div className="flex items-center gap-3">
          <button
            type="submit"
            disabled={busy || !dirty || stale}
            className="rounded-md bg-crimson px-3 py-2 text-sm font-medium text-white hover:bg-crimson-dark disabled:opacity-40 dark:bg-crimson-light dark:text-gray-950"
          >
            {busy ? 'Saving…' : 'Save archive settings'}
          </button>
          {settings.updated_at && (
            <span className="text-xs text-gray-500 dark:text-gray-400">
              Last saved {new Date(settings.updated_at).toLocaleString()}
            </span>
          )}
        </div>
      </form>
    </section>
  );
}
