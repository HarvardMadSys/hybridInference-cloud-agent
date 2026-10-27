'use client';

import { useId, useState, type FormEvent } from 'react';
import { useT } from '@/components/providers';
import {
  createModelConnection,
  updateModelConnection,
  type ModelConnection,
  type ModelProtocol,
} from '@/lib/api/modelConnections';

const inputClass = 'ui-input mt-1 block min-h-11 w-full rounded-lg';

export function ModelConnectionForm({
  connection,
  onSaved,
  onCancel,
}: {
  connection?: ModelConnection;
  onSaved: (connection: ModelConnection) => void;
  onCancel: () => void;
}) {
  const t = useT();
  const id = useId();
  const [name, setName] = useState(connection?.name ?? '');
  const [url, setUrl] = useState(connection?.base_url ?? '');
  const [key, setKey] = useState('');
  const [models, setModels] = useState(connection?.models.join('\n') ?? '');
  const [protocols, setProtocols] = useState<ModelProtocol[]>(
    connection?.protocols ?? ['chat_completions'],
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);

  function toggle(protocol: ModelProtocol) {
    setProtocols((current) =>
      current.includes(protocol)
        ? current.filter((value) => value !== protocol)
        : [...current, protocol],
    );
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy || !protocols.length) return;
    setBusy(true);
    setError(false);
    const fields = {
      name: name.trim(),
      base_url: url.trim(),
      models: [
        ...new Set(
          models
            .split('\n')
            .map((model) => model.trim())
            .filter(Boolean),
        ),
      ],
      protocols,
    };
    try {
      let saved: ModelConnection;
      if (connection) {
        const changes = { ...fields, ...(key.trim() ? { api_key: key.trim() } : {}) };
        saved = await updateModelConnection(connection.id, changes);
      } else {
        saved = await createModelConnection({ ...fields, api_key: key.trim() });
      }
      setKey('');
      onSaved(saved);
    } catch {
      setError(true);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      onSubmit={(event) => void save(event)}
      className="mt-4 space-y-4 rounded-xl border border-border p-4"
    >
      <fieldset disabled={busy} className="space-y-4">
        <legend className="sr-only">
          {t(connection ? 'connections.edit' : 'connections.add')}
        </legend>
        <label className="block text-sm font-medium">
          {t('connections.name')}
          <input
            className={inputClass}
            value={name}
            onChange={(event) => setName(event.target.value)}
            required
            maxLength={64}
            autoComplete="off"
          />
        </label>
        <label className="block text-sm font-medium">
          {t('connections.url')}
          <input
            className={inputClass}
            type="url"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            required
            maxLength={512}
            placeholder="https://api.example.com/v1"
            aria-describedby={`${id}-url`}
            autoComplete="off"
          />
        </label>
        <p id={`${id}-url`} className="text-xs leading-5 text-muted">
          {t('connections.urlHint')}
        </p>
        <label className="block text-sm font-medium">
          {t('connections.key')}
          <input
            className={inputClass}
            type="password"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            required={!connection}
            maxLength={4096}
            autoComplete="new-password"
            spellCheck={false}
            aria-describedby={`${id}-key`}
          />
        </label>
        <p id={`${id}-key`} className="text-xs leading-5 text-muted">
          {connection ? `${t('connections.keyHint')} ` : ''}
          {t('connections.secretHint')}
        </p>
        <fieldset className="space-y-2">
          <legend className="mb-2 text-sm font-medium">{t('connections.protocols')}</legend>
          {(['chat_completions', 'responses'] as const).map((protocol) => (
            <label key={protocol} className="flex min-h-9 items-center gap-2 text-sm">
              <input
                type="checkbox"
                checked={protocols.includes(protocol)}
                onChange={() => toggle(protocol)}
              />
              {t(protocol === 'responses' ? 'connections.responses' : 'connections.chat')}
            </label>
          ))}
          <p className="text-xs leading-5 text-muted">{t('connections.protocolHint')}</p>
        </fieldset>
        <label className="block text-sm font-medium">
          {t('connections.models')}
          <textarea
            className={inputClass}
            value={models}
            onChange={(event) => setModels(event.target.value)}
            required
            rows={4}
            spellCheck={false}
            aria-describedby={`${id}-models`}
          />
        </label>
        <p id={`${id}-models`} className="text-xs leading-5 text-muted">
          {t('connections.modelsHint')}
        </p>
        {connection ? (
          <p className="text-sm leading-6 text-muted">{t('connections.editHint')}</p>
        ) : null}
      </fieldset>
      {error ? (
        <p role="alert" className="text-sm text-status-error">
          {t('connections.error')}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <button
          type="submit"
          disabled={
            busy ||
            !protocols.length ||
            !name.trim() ||
            !models.trim() ||
            (!connection && !key.trim())
          }
          className="ui-button ui-button-primary min-h-11"
        >
          {t(busy ? 'connections.busy' : 'connections.save')}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onCancel}
          className="ui-button ui-button-secondary min-h-11"
        >
          {t('connections.cancel')}
        </button>
      </div>
    </form>
  );
}
