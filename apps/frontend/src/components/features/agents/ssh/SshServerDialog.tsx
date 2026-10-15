'use client';

import { useState } from 'react';
import { useT } from '@/components/providers';
import {
  createSshServer,
  updateSshServer,
  type SshServerApi,
  type SshServerInput,
} from '@/lib/api/sshServers';
import { getErrorMessage } from '@/lib/utils/errors';
import { BUTTON, FIELD, PRIMARY, SshDialogFrame } from './SshDialogFrame';

/** Add a server, or change one; changing how to reach it sends it back to checking. */
export function SshServerDialog({
  server,
  onClose,
  onSaved,
}: {
  /** Null adds a new server. */
  server: SshServerApi | null;
  onClose: () => void;
  onSaved: (server: SshServerApi, created: boolean) => void;
}) {
  const t = useT();
  const [name, setName] = useState(server?.name ?? '');
  const [address, setAddress] = useState(server?.address ?? '');
  const [port, setPort] = useState(String(server?.port ?? 22));
  const [username, setUsername] = useState(server?.username ?? '');
  const [description, setDescription] = useState(server?.description ?? '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save() {
    if (busy) return;
    setBusy(true);
    setError(null);
    const input: SshServerInput = {
      name: name.trim(),
      address: address.trim(),
      port: Number(port),
      username: username.trim(),
      description: description.trim(),
    };
    try {
      if (server) {
        const changes = Object.fromEntries(
          Object.entries(input).filter(
            ([key, value]) => value !== server[key as keyof SshServerInput],
          ),
        ) as Partial<SshServerInput>;
        onSaved(
          Object.keys(changes).length ? await updateSshServer(server, changes) : server,
          false,
        );
      } else {
        onSaved(await createSshServer(input), true);
      }
    } catch (cause) {
      setError(getErrorMessage(cause));
      setBusy(false);
    }
  }

  return (
    <SshDialogFrame
      title={server ? t('ssh.formEditTitle', { name: server.name }) : t('ssh.formAddTitle')}
      closeLabel={t('ssh.close')}
      busy={busy}
      onClose={onClose}
      initialFocus="input"
    >
      <form
        className="mt-5 space-y-4"
        onSubmit={(event) => {
          event.preventDefault();
          void save();
        }}
      >
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
          {t('ssh.name')}
          <input
            name="name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            maxLength={32}
            pattern="[a-z][a-z0-9\-]{1,31}"
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
            className={FIELD}
          />
          <span className="mt-1 block text-xs font-normal text-gray-500 dark:text-gray-400">
            {t('ssh.nameHint')}
          </span>
        </label>
        <div className="grid grid-cols-[1fr_6rem] gap-3">
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
            {t('ssh.address')}
            <input
              name="address"
              value={address}
              onChange={(event) => setAddress(event.target.value)}
              required
              maxLength={253}
              autoComplete="off"
              spellCheck={false}
              placeholder={t('ssh.addressHint')}
              disabled={busy}
              className={FIELD}
            />
          </label>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
            {t('ssh.port')}
            <input
              name="port"
              type="number"
              min={1}
              max={65535}
              value={port}
              onChange={(event) => setPort(event.target.value)}
              required
              disabled={busy}
              className={FIELD}
            />
          </label>
        </div>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
          {t('ssh.username')}
          <input
            name="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            required
            maxLength={32}
            pattern="[A-Za-z0-9._][A-Za-z0-9._\-]{0,31}"
            autoComplete="off"
            spellCheck={false}
            disabled={busy}
            className={FIELD}
          />
        </label>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
          {t('ssh.note')}
          <input
            name="description"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            maxLength={200}
            placeholder={t('ssh.notePlaceholder')}
            disabled={busy}
            className={FIELD}
          />
        </label>
        {server ? (
          <p className="text-xs text-gray-500 dark:text-gray-400">{t('ssh.reachChanged')}</p>
        ) : null}
        {error ? (
          <p role="alert" className="text-sm text-red-600 dark:text-red-400">
            {error}
          </p>
        ) : null}
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} disabled={busy} className={BUTTON}>
            {t('ssh.cancel')}
          </button>
          <button type="submit" disabled={busy} className={PRIMARY}>
            {t('ssh.save')}
          </button>
        </div>
      </form>
    </SshDialogFrame>
  );
}
