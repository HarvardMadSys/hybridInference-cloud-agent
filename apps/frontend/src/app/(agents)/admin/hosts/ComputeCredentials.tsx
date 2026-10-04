import type { ComputeConnection, ComputeProvider } from '@/lib/api/compute';

import { Field, initialFields, inputClass, ProviderFields } from './compute-ui';

export const NEW_CREDENTIAL = '__new__';

export interface CredentialDraft {
  selection: string;
  name: string;
  values: Record<string, string>;
  secret: string;
}

export function credentialLabel(providerId: string, providerName?: string): string {
  return providerId === 'existing_ssh' ? 'SSH key' : `${providerName ?? providerId} account`;
}

export function initialCredential(
  provider: ComputeProvider | undefined,
  connections: ComputeConnection[],
): CredentialDraft {
  const saved = connections.filter((item) => item.provider_id === provider?.id);
  return {
    selection: saved.length === 1 ? saved[0].id : saved.length ? '' : NEW_CREDENTIAL,
    name: '',
    values: initialFields(provider?.connection_fields ?? []),
    secret: '',
  };
}

export function CredentialFields({
  provider,
  value,
  onChange,
  editing = false,
  suggestedName,
}: {
  provider: ComputeProvider;
  value: CredentialDraft;
  onChange: (value: CredentialDraft) => void;
  editing?: boolean;
  suggestedName?: string;
}) {
  const ssh = provider.id === 'existing_ssh';
  return (
    <div className="space-y-4">
      <Field label={ssh ? 'Key name' : 'Account name'}>
        <input
          className={inputClass}
          value={value.name}
          required={editing}
          placeholder={suggestedName}
          onChange={(event) => onChange({ ...value, name: event.target.value })}
        />
      </Field>
      <Field label={provider.secret_label ?? 'Credential'}>
        {ssh ? (
          <textarea
            className={`${inputClass} font-mono`}
            rows={4}
            required={!editing}
            value={value.secret}
            onChange={(event) => onChange({ ...value, secret: event.target.value })}
            autoComplete="off"
            autoCorrect="off"
            spellCheck={false}
            placeholder={editing ? 'Leave blank to keep the current key' : undefined}
          />
        ) : (
          <input
            className={inputClass}
            type="password"
            required={!editing}
            value={value.secret}
            onChange={(event) => onChange({ ...value, secret: event.target.value })}
            autoComplete="new-password"
            placeholder={editing ? 'Leave blank to keep the current credential' : undefined}
          />
        )}
      </Field>
      <ProviderFields
        fields={provider.connection_fields}
        values={value.values}
        onChange={(values) => onChange({ ...value, values })}
      />
    </div>
  );
}

export function CredentialPicker({
  provider,
  connections,
  value,
  onChange,
  serverName,
}: {
  provider: ComputeProvider;
  connections: ComputeConnection[];
  value: CredentialDraft;
  onChange: (value: CredentialDraft) => void;
  serverName: string;
}) {
  const saved = connections.filter((item) => item.provider_id === provider.id);
  const label = credentialLabel(provider.id, provider.label);
  const selected = saved.find((item) => item.id === value.selection);
  return (
    <fieldset className="space-y-3 rounded-xl border border-gray-200 p-4 dark:border-white/10">
      <legend className="px-1 text-sm font-medium">{label}</legend>
      {saved.length > 0 && (
        <select
          aria-label={`Use ${label}`}
          className={inputClass}
          required
          value={value.selection}
          onChange={(event) => onChange({ ...value, selection: event.target.value, secret: '' })}
        >
          <option value="">Select…</option>
          {saved.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
          <option value={NEW_CREDENTIAL}>
            Add a new {provider.id === 'existing_ssh' ? 'SSH key' : 'account'}…
          </option>
        </select>
      )}
      {value.selection === NEW_CREDENTIAL && (
        <CredentialFields
          provider={provider}
          value={value}
          onChange={onChange}
          suggestedName={`${serverName || 'Server'} ${provider.id === 'existing_ssh' ? 'key' : 'account'}`}
        />
      )}
      {selected?.blocked_reasons.map((reason) => (
        <p key={reason.code} className="text-sm text-amber-700 dark:text-amber-300">
          {reason.message}
        </p>
      ))}
    </fieldset>
  );
}
