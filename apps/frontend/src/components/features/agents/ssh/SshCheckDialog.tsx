'use client';

import { useState } from 'react';
import { useT } from '@/components/providers';
import { scanSshServer, verifySshServer, type SshServerApi } from '@/lib/api/sshServers';
import { getErrorMessage } from '@/lib/utils/errors';
import { BUTTON, PRIMARY, SshDialogFrame } from './SshDialogFrame';

/** Where each host key type lives on a stock OpenSSH server. */
const HOST_KEY_FILES: Record<string, string> = {
  'ssh-ed25519': 'ssh_host_ed25519_key.pub',
  'ecdsa-sha2-nistp256': 'ssh_host_ecdsa_key.pub',
  'ecdsa-sha2-nistp384': 'ssh_host_ecdsa_key.pub',
  'ecdsa-sha2-nistp521': 'ssh_host_ecdsa_key.pub',
  'ssh-rsa': 'ssh_host_rsa_key.pub',
};

function fingerprintCommand(type: string): string {
  return `ssh-keygen -lf /etc/ssh/${HOST_KEY_FILES[type] ?? 'ssh_host_ed25519_key.pub'}`;
}

/**
 * Install the platform's key, confirm the host key, log in once.
 *
 * A key rotation against an already pinned host key skips straight to the
 * login: the host key is not what changed.
 */
export function SshCheckDialog({
  server: initial,
  onClose,
  onChanged,
  refresh,
}: {
  server: SshServerApi;
  onClose: () => void;
  onChanged: (server: SshServerApi) => void;
  /** The latest copy of this server, after a failed check moved its revision. */
  refresh: () => Promise<SshServerApi | undefined>;
}) {
  const t = useT();
  const [server, setServer] = useState(initial);
  const [fingerprint, setFingerprint] = useState(initial.scan?.host_keys[0]?.fingerprint ?? '');
  const [busy, setBusy] = useState<'scan' | 'verify' | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [copied, setCopied] = useState(false);
  const rotating = server.pending_install_command !== null;
  const command = server.pending_install_command ?? server.install_command;
  const pinnedRotation = rotating && server.host_key !== null;

  function accept(next: SshServerApi) {
    setServer(next);
    onChanged(next);
  }

  async function run(step: 'scan' | 'verify', action: () => Promise<SshServerApi>) {
    if (busy) return;
    setBusy(step);
    setError(null);
    try {
      const next = await action();
      accept(next);
      if (step === 'scan') setFingerprint(next.scan?.host_keys[0]?.fingerprint ?? '');
      else setDone(true);
    } catch (cause) {
      setError(getErrorMessage(cause));
      const fresh = await refresh().catch(() => undefined);
      if (fresh) accept(fresh);
    } finally {
      setBusy(null);
    }
  }

  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  return (
    <SshDialogFrame
      title={t('ssh.checkTitle', { name: server.name })}
      closeLabel={t('ssh.close')}
      busy={busy !== null}
      onClose={onClose}
    >
      {done ? (
        <div className="mt-5">
          <p role="status" className="text-sm text-emerald-700 dark:text-emerald-300">
            {t('ssh.verified', { name: server.name })}
          </p>
          <div className="mt-5 flex justify-end">
            <button type="button" onClick={onClose} className={PRIMARY}>
              {t('ssh.close')}
            </button>
          </div>
        </div>
      ) : (
        <div className="mt-5 space-y-5 text-sm text-gray-700 dark:text-gray-200">
          <section>
            <h3 className="font-medium">{t('ssh.stepInstall', { user: server.username })}</h3>
            {rotating ? (
              <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">{t('ssh.rotateHint')}</p>
            ) : null}
            <div className="mt-2 flex items-start gap-2">
              <code className="min-w-0 flex-1 break-all rounded-lg bg-gray-50 px-3 py-2 font-mono text-xs text-gray-800 dark:bg-white/5 dark:text-gray-200">
                {command}
              </code>
              <button type="button" onClick={() => void copy()} className={BUTTON}>
                {copied ? t('ssh.copied') : t('ssh.copy')}
              </button>
            </div>
          </section>

          {pinnedRotation && server.host_key ? (
            <div className="flex justify-end">
              <button
                type="button"
                disabled={busy !== null}
                onClick={() =>
                  void run('verify', () =>
                    verifySshServer(server, server.host_key?.fingerprint ?? ''),
                  )
                }
                className={PRIMARY}
              >
                {busy === 'verify' ? t('ssh.verifying') : t('ssh.testNewKey')}
              </button>
            </div>
          ) : (
            <>
              <section>
                <h3 className="font-medium">{t('ssh.stepScan')}</h3>
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => void run('scan', () => scanSshServer(server))}
                  className={`${BUTTON} mt-2`}
                >
                  {busy === 'scan' ? t('ssh.scanning') : t('ssh.scan')}
                </button>
              </section>
              {server.scan ? (
                <section>
                  <h3 className="font-medium">{t('ssh.stepConfirm')}</h3>
                  <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                    {t('ssh.addressFound', { address: server.scan.address })}
                  </p>
                  <fieldset className="mt-2 space-y-2" disabled={busy !== null}>
                    {server.scan.host_keys.map((key) => (
                      <label key={key.fingerprint} className="flex items-start gap-2">
                        <input
                          type="radio"
                          name="fingerprint"
                          checked={fingerprint === key.fingerprint}
                          onChange={() => setFingerprint(key.fingerprint)}
                          className="mt-1 accent-crimson"
                        />
                        <span className="min-w-0">
                          <span className="block break-all font-mono text-xs">
                            {key.fingerprint}
                          </span>
                          <span className="block text-xs text-gray-500 dark:text-gray-400">
                            {key.type} ·{' '}
                            {t('ssh.fingerprintHint', { command: fingerprintCommand(key.type) })}
                          </span>
                        </span>
                      </label>
                    ))}
                  </fieldset>
                  <div className="mt-4 flex justify-end">
                    <button
                      type="button"
                      disabled={busy !== null || !fingerprint}
                      onClick={() => void run('verify', () => verifySshServer(server, fingerprint))}
                      className={PRIMARY}
                    >
                      {busy === 'verify' ? t('ssh.verifying') : t('ssh.verify')}
                    </button>
                  </div>
                </section>
              ) : null}
            </>
          )}
          {error ? (
            <p role="alert" className="text-sm text-red-600 dark:text-red-400">
              {error}
            </p>
          ) : null}
        </div>
      )}
    </SshDialogFrame>
  );
}
