'use client';

import { useCallback, useEffect, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import {
  getCloudLabDefaults,
  saveCloudLabDefaults,
  type CloudLabDefaults,
} from '@/lib/api/compute';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import { SettingsSection } from '../SettingsSection';

const SSH_PUBLIC_KEY_ALGORITHM =
  /^(ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|ssh-rsa)$/;

function sshPublicKeyError(value: string): string | null {
  const key = value.trim();
  if (!key) return null;
  if (/[\r\n]/.test(value)) return 'SSH public key must be one OpenSSH public key.';
  const parts = key.split(/\s+/);
  const comment = parts.slice(2).join(' ');
  if (
    parts.length < 2 ||
    !SSH_PUBLIC_KEY_ALGORITHM.test(parts[0]) ||
    !/^[A-Za-z0-9+/]+={0,2}$/.test(parts[1]) ||
    comment.length > 128
  ) {
    return 'SSH public key must be one OpenSSH public key.';
  }
  return null;
}

function AccessDenied() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8">
      <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
        Admin access required
      </h1>
      <p className="text-sm text-gray-500 dark:text-gray-400">
        CloudLab settings are visible to admins only.
      </p>
    </div>
  );
}

export default function CloudLabSettings({
  onAccessDenied,
}: {
  onAccessDenied?: (cause: unknown) => void;
}) {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) {
    return <AccessDenied />;
  }
  return <CloudLabSettingsForm key={state.user?.id ?? 'admin'} onAccessDenied={onAccessDenied} />;
}

function CloudLabSettingsForm({ onAccessDenied }: { onAccessDenied?: (cause: unknown) => void }) {
  const [saved, setSaved] = useState<CloudLabDefaults | null>(null);
  const [draft, setDraft] = useState<CloudLabDefaults | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [denied, setDenied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [advancedOpen, setAdvancedOpen] = useState(false);

  const fail = useCallback(
    (cause: unknown) => {
      if (cause instanceof APIError && (cause.statusCode === 401 || cause.statusCode === 403)) {
        setDenied(true);
        setSaved(null);
        setDraft(null);
        setError(null);
        setNotice(null);
        onAccessDenied?.(cause);
        return;
      }
      setError(getErrorMessage(cause));
    },
    [onAccessDenied],
  );

  const load = useCallback(
    async (signal?: AbortSignal) => {
      setLoading(true);
      try {
        const response = await getCloudLabDefaults(signal);
        if (signal?.aborted) return;
        setSaved(response.defaults);
        setDraft(response.defaults);
        setError(null);
      } catch (cause) {
        if (!signal?.aborted) fail(cause);
      } finally {
        if (!signal?.aborted) setLoading(false);
      }
    },
    [fail],
  );

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  function updateDraft(update: Partial<CloudLabDefaults>) {
    setDraft((current) => (current ? { ...current, ...update } : current));
    setAdvancedOpen(false);
  }

  async function submit() {
    if (!draft || busy) return;
    if (!Number.isInteger(draft.duration) || draft.duration < 1 || draft.duration > 168) {
      setError('Initial duration must be a whole number from 1 to 168 hours.');
      return;
    }
    const keyError = sshPublicKeyError(draft.ssh_public_key ?? '');
    if (keyError) {
      setError(keyError);
      setAdvancedOpen(true);
      return;
    }
    setBusy(true);
    setAdvancedOpen(false);
    setError(null);
    setNotice(null);
    try {
      const response = await saveCloudLabDefaults(draft);
      setSaved(response.defaults);
      setDraft(response.defaults);
      setNotice('CloudLab defaults saved. New server forms use them immediately.');
    } catch (cause) {
      if (cause instanceof APIError && cause.statusCode === 422) setAdvancedOpen(true);
      fail(cause);
    } finally {
      setBusy(false);
    }
  }

  if (denied) return <AccessDenied />;

  const changed = draft !== null && JSON.stringify(draft) !== JSON.stringify(saved);
  const inputClass = 'ui-input min-h-11 w-full';

  return (
    <>
      <div className="mt-6">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-50">CloudLab defaults</h2>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Prefill new CloudLab server forms. An administrator can still change any value for an
          individual allocation.
        </p>
      </div>
      {error && (
        <div
          role="alert"
          className="mt-6 flex items-center justify-between gap-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-400"
        >
          <span>{error}</span>
          {!draft && (
            <button
              type="button"
              className="shrink-0 font-medium underline"
              onClick={() => void load()}
            >
              Retry
            </button>
          )}
        </div>
      )}
      {notice && (
        <p role="status" className="mt-6 text-sm text-gray-600 dark:text-gray-300">
          {notice}
        </p>
      )}
      {loading && (
        <p role="status" className="mt-8 text-sm text-gray-500 dark:text-gray-400">
          Loading CloudLab settings…
        </p>
      )}
      {draft && (
        <form
          className="mt-8 space-y-8"
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <fieldset disabled={busy} className="space-y-8">
            <SettingsSection
              title="Important"
              description="Choose the project and initial lifetime for new CloudLab servers."
            >
              <section className="ui-card space-y-4 p-5" aria-labelledby="cloud-resource-heading">
                <h3
                  id="cloud-resource-heading"
                  className="border-b border-border pb-3 text-sm font-semibold text-foreground"
                >
                  Cloud resource
                </h3>
                <div className="grid gap-4 sm:grid-cols-2">
                  <label className="space-y-2 text-sm">
                    <span className="font-medium">CloudLab project</span>
                    <input
                      aria-label="CloudLab project"
                      className={inputClass}
                      maxLength={128}
                      value={draft.project}
                      onChange={(event) => updateDraft({ project: event.target.value })}
                    />
                    <span className="block text-xs text-gray-500 dark:text-gray-400">
                      Project that will own new experiments.
                    </span>
                  </label>
                  <label className="space-y-2 text-sm">
                    <span className="font-medium">Initial duration (hours)</span>
                    <input
                      aria-label="Initial duration (hours)"
                      className={inputClass}
                      type="number"
                      min={1}
                      max={168}
                      step={1}
                      required
                      value={draft.duration}
                      onChange={(event) => updateDraft({ duration: Number(event.target.value) })}
                    />
                  </label>
                </div>
              </section>
            </SettingsSection>

            <SettingsSection
              title="Advanced"
              description="Customize the machine template and SSH access used for new servers."
              open={advancedOpen}
            >
              <section
                className="ui-card space-y-4 p-5"
                aria-labelledby="machine-template-heading"
                onInvalidCapture={(event) => {
                  const section = event.currentTarget.closest('details');
                  if (section) section.open = true;
                }}
              >
                <h3
                  id="machine-template-heading"
                  className="border-b border-border pb-3 text-sm font-semibold text-foreground"
                >
                  Machine template and access
                </h3>
                <div className="grid gap-4 sm:grid-cols-2">
                  <label className="space-y-2 text-sm">
                    <span className="font-medium">Template project</span>
                    <input
                      aria-label="Template project"
                      className={inputClass}
                      maxLength={128}
                      value={draft.profile_project}
                      onChange={(event) => updateDraft({ profile_project: event.target.value })}
                    />
                    <span className="block text-xs text-gray-500 dark:text-gray-400">
                      Project that publishes the CloudLab profile.
                    </span>
                  </label>
                  <label className="space-y-2 text-sm">
                    <span className="font-medium">Template name</span>
                    <input
                      aria-label="Template name"
                      className={inputClass}
                      maxLength={128}
                      value={draft.profile_name}
                      onChange={(event) => updateDraft({ profile_name: event.target.value })}
                    />
                    <span className="block text-xs text-gray-500 dark:text-gray-400">
                      Name of the CloudLab profile to use.
                    </span>
                  </label>
                </div>
                <label className="block max-w-xl space-y-2 text-sm">
                  <span className="font-medium">CloudLab SSH user</span>
                  <input
                    aria-label="CloudLab SSH user"
                    className={inputClass}
                    maxLength={128}
                    autoComplete="off"
                    value={draft.ssh_username}
                    onChange={(event) => updateDraft({ ssh_username: event.target.value })}
                  />
                </label>
                <label className="block space-y-2 text-sm">
                  <span className="font-medium">SSH public key</span>
                  <textarea
                    aria-label="CloudLab SSH public key"
                    className={`${inputClass} font-mono`}
                    rows={3}
                    maxLength={8192}
                    spellCheck={false}
                    autoComplete="off"
                    autoCorrect="off"
                    placeholder="ssh-ed25519 AAAA..."
                    value={draft.ssh_public_key ?? ''}
                    onChange={(event) => updateDraft({ ssh_public_key: event.target.value })}
                  />
                  <span className="block text-xs text-gray-500 dark:text-gray-400">
                    Installed for this user on new experiments. Leave blank to use keys already on
                    the CloudLab account.
                  </span>
                </label>
              </section>
            </SettingsSection>

            <div className="flex items-center justify-end">
              <button
                type="submit"
                disabled={!changed || busy}
                className="ui-button ui-button-primary min-h-11 px-5"
              >
                {busy ? 'Saving…' : 'Save defaults'}
              </button>
            </div>
          </fieldset>
        </form>
      )}
    </>
  );
}
