'use client';

import { useEffect, useId, useRef, useState, type ReactNode, type Ref } from 'react';

import {
  createComputeServer,
  getComputeOperations,
  importComputeResource,
  saveComputeConnection,
  type ComputeCatalog,
  type ComputeConnection,
  type ComputeOperation,
  type ComputeProvider,
  type ServerRequest,
} from '@/lib/api/compute';
import { getErrorMessage } from '@/lib/utils/errors';

import {
  CredentialPicker,
  initialCredential,
  NEW_CREDENTIAL,
  type CredentialDraft,
} from './ComputeCredentials';
import {
  buttonClass,
  ErrorNotice,
  Field,
  initialFields,
  inputClass,
  parseFields,
  primaryClass,
  ProviderFieldError,
  ProviderFields,
} from './compute-ui';

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section
      className="space-y-4 border-t border-gray-200 pt-5 dark:border-white/10"
      aria-label={title}
    >
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </section>
  );
}

// Where CloudLab's small-lan profile, and deploy/cloudlab/cloud-agent-node.py,
// mount a new experiment's data disk unless told otherwise.
const CLOUDLAB_DATA_MOUNT = '/mydata';

function defaultSandboxStoragePath(providerId: string, mode: 'attach' | 'provision'): string {
  return providerId === 'cloudlab' && mode === 'provision' ? CLOUDLAB_DATA_MOUNT : '';
}

function parseSandboxStoragePath(value: string): string {
  const path = value.trim();
  const invalid = (message: string): never => {
    throw new ProviderFieldError('sandbox_storage_path', message);
  };
  if (!path) return invalid('Sandbox storage path is required.');
  if (path.length > 4096) {
    return invalid('Sandbox storage path must be 4096 characters or fewer.');
  }
  if (!/^\/[A-Za-z0-9/_-]*$/.test(path) || path.includes('//')) {
    return invalid(
      'Sandbox storage path must be an absolute path using letters, numbers, /, _ or - with no repeated separators.',
    );
  }
  const normalized = path.replace(/\/$/, '') || '/';
  const systemRoots = [
    '/',
    '/home',
    '/media',
    '/mnt',
    '/opt',
    '/root',
    '/srv',
    '/tmp',
    '/var',
    '/var/lib',
  ];
  const protectedTrees = [
    '/bin',
    '/boot',
    '/dev',
    '/etc',
    '/lib',
    '/lib64',
    '/proc',
    '/run',
    '/sbin',
    '/sys',
    '/usr',
    '/var/lib/docker',
    '/var/lib/containerd',
    '/var/lib/cloud-agent',
    '/var/lib/hybridinference',
  ];
  if (
    systemRoots.includes(normalized) ||
    protectedTrees.some((root) => normalized === root || normalized.startsWith(`${root}/`))
  ) {
    return invalid(
      'Sandbox storage path must point to a dedicated mounted data disk outside system and service directories.',
    );
  }
  return normalized;
}

export default function AddComputeServer({
  catalog,
  connections,
  onOperation,
  onConnectionSaved,
  onConfigureSetup,
  configureSetupButtonRef,
}: {
  catalog: ComputeCatalog;
  connections: ComputeConnection[];
  onOperation: (operation: ComputeOperation, submitted: boolean) => void;
  onConnectionSaved: (connection: ComputeConnection) => void;
  onConfigureSetup?: () => void;
  configureSetupButtonRef?: Ref<HTMLButtonElement>;
}) {
  const form = useRef<HTMLFormElement>(null);
  const [newConnections, setNewConnections] = useState<ComputeConnection[]>([]);
  const availableConnections = [
    ...newConnections,
    ...connections.filter(
      (connection) => !newConnections.some((saved) => saved.id === connection.id),
    ),
  ];
  const [providerId, setProviderId] = useState(catalog.providers[0]?.id ?? '');
  const provider = catalog.providers.find((item) => item.id === providerId);
  const [mode, setMode] = useState<'attach' | 'provision'>(provider?.modes[0] ?? 'attach');
  const [name, setName] = useState('');
  // Null until the administrator types one: a new CloudLab experiment's disk
  // is where small-lan mounts it, and that follows the server type until then.
  const [sandboxStorageInput, setSandboxStorageInput] = useState<string | null>(null);
  const sandboxStoragePath = sandboxStorageInput ?? defaultSandboxStoragePath(providerId, mode);
  const storageId = useId();
  const [account, setAccount] = useState(() => initialCredential(provider, connections));
  const sshProvider = catalog.providers.find((item) => item.id === 'existing_ssh');
  const hostKeySourceField = provider?.fields.find((field) => field.key === 'host_key_source');
  const supportsUncheckedCloudLabSsh =
    provider?.id === 'cloudlab' &&
    hostKeySourceField?.default === 'none' &&
    hostKeySourceField.options?.some((option) => option.value === 'none') === true;
  const deploymentSshProvider =
    supportsUncheckedCloudLabSsh && sshProvider
      ? { ...sshProvider, connection_fields: [] }
      : sshProvider;
  const [ssh, setSsh] = useState(() => initialCredential(sshProvider, connections));
  const deploymentConfigured = catalog.deployment_profiles.length > 0;
  const [values, setValues] = useState(() => initialFields(provider?.fields ?? []));
  const skipsCloudLabHostKeyCheck =
    supportsUncheckedCloudLabSsh &&
    (mode === 'attach' || values.host_key_source !== 'registration');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [accountError, setAccountError] = useState<string | null>(null);
  const [sshError, setSshError] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [observeOnly, setObserveOnly] = useState(false);
  // Retained after an ambiguous network result; repeated submits use the same intent.
  const requestIds = useRef<Partial<Record<'deploy' | 'import', string>>>({});
  const fields = (provider?.fields ?? []).filter(
    (field) =>
      (!field.modes || field.modes.includes(mode)) && !(observeOnly && field.deployment_only),
  );
  const mainFields = fields.filter((field) => !field.advanced || field.required);
  const advancedFields = fields.filter((field) => field.advanced && !field.required);
  const resourceFields = mainFields.filter((field) => !field.section);
  const templateFields = mainFields.filter((field) => field.section === 'template');
  const accessFields = mainFields.filter((field) => field.section === 'access');
  const cloud = provider?.id !== 'existing_ssh';
  const needsSshKey = provider?.requires_ssh_connection;
  const canObserve = mode === 'attach' && provider?.capabilities.import;
  const changed = () => {
    requestIds.current = {};
    setError(null);
    setAccountError(null);
    setSshError(null);
    setFieldErrors({});
  };
  useEffect(() => {
    if (Object.keys(fieldErrors).length) {
      form.current?.querySelector<HTMLElement>('[aria-invalid="true"]')?.focus();
    }
  }, [fieldErrors]);

  async function saveCredential(
    draft: CredentialDraft,
    owner: ComputeProvider,
    update: (draft: CredentialDraft) => void,
  ): Promise<string> {
    if (draft.selection !== NEW_CREDENTIAL) {
      if (!draft.selection)
        throw new Error(`Select a ${owner.id === 'existing_ssh' ? 'SSH key' : 'cloud account'}.`);
      return draft.selection;
    }
    const { connection } = await saveComputeConnection({
      name:
        draft.name.trim() ||
        `${name.trim() || 'Server'} ${owner.id === 'existing_ssh' ? 'key' : 'account'}`,
      provider_id: owner.id,
      configuration: parseFields(owner.connection_fields, draft.values),
      secret: draft.secret,
    });
    // Keep each successful save across partial failures; never send a secret with a server request.
    update({ ...draft, selection: connection.id, secret: '' });
    setNewConnections((current) => [
      connection,
      ...current.filter((item) => item.id !== connection.id),
    ]);
    onConnectionSaved(connection);
    return connection.id;
  }

  async function submit() {
    if (busy || !provider) return;
    setBusy(true);
    setError(null);
    setAccountError(null);
    setSshError(null);
    setFieldErrors({});
    const action = observeOnly ? 'import' : 'deploy';
    const requestId = requestIds.current[action] ?? crypto.randomUUID();
    requestIds.current[action] = requestId;
    let stage: 'fields' | 'account' | 'ssh' | 'server' = 'fields';
    let submitted = false;
    try {
      if (!observeOnly && !deploymentConfigured) {
        throw new Error('Configure server setup before this server can run sandboxes.');
      }
      const configuration = parseFields(fields, values);
      const storagePath = observeOnly ? undefined : parseSandboxStoragePath(sandboxStoragePath);
      if (needsSshKey && !observeOnly && !deploymentSshProvider) {
        throw new Error('SSH access is not configured.');
      }
      stage = 'account';
      const connectionId = await saveCredential(account, provider, setAccount);
      let sshConnectionId: string | undefined;
      if (needsSshKey && !observeOnly) {
        stage = 'ssh';
        sshConnectionId = await saveCredential(ssh, deploymentSshProvider!, setSsh);
      }
      const body: ServerRequest = {
        request_id: requestId,
        name,
        provider_id: provider.id,
        mode,
        connection_id: connectionId,
        ...(sshConnectionId ? { ssh_connection_id: sshConnectionId } : {}),
        configuration,
        ...(storagePath ? { sandbox_storage_path: storagePath } : {}),
      };
      stage = 'server';
      submitted = true;
      const { operation } = await (observeOnly ? importComputeResource : createComputeServer)(body);
      onOperation(operation, true);
    } catch (error) {
      // A lost response does not justify creating a second resource. Ask by the
      // stable request ID before allowing a retry with the same payload.
      try {
        const recovered = submitted ? await getComputeOperations(requestId) : { operations: [] };
        const operation = recovered.operations.find((item) => item.request_id === requestId);
        if (operation) {
          onOperation(operation, true);
          return;
        }
      } catch {
        /* The same request ID remains available for reconciliation. */
      }
      const message = getErrorMessage(error);
      if (stage === 'fields' && error instanceof ProviderFieldError) {
        setFieldErrors({ [error.field]: message });
        if (advancedFields.some((field) => field.key === error.field)) setAdvancedOpen(true);
      } else if (stage === 'account') setAccountError(message);
      else if (stage === 'ssh') setSshError(message);
      else setError(message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      ref={form}
      className="space-y-5"
      onChange={changed}
      onInvalidCapture={(event) => {
        const details = (event.target as HTMLElement).closest('details');
        if (details) {
          // Reveal native-validation failures before the browser focuses them.
          details.open = true;
          setAdvancedOpen(true);
        }
      }}
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <fieldset disabled={busy} className="space-y-5">
        <fieldset>
          <legend className="mb-2 text-sm font-medium">Server type</legend>
          <div className="grid gap-3 sm:grid-cols-2">
            {catalog.providers.map((item) => (
              <label key={item.id} className="cursor-pointer">
                <input
                  className="peer sr-only"
                  type="radio"
                  name="provider"
                  value={item.id}
                  checked={providerId === item.id}
                  onChange={() => {
                    setProviderId(item.id);
                    setMode(item.modes.includes('provision') ? 'provision' : 'attach');
                    setObserveOnly(false);
                    setAdvancedOpen(false);
                    setValues(initialFields(item.fields));
                    setAccount(initialCredential(item, availableConnections));
                    setSsh(initialCredential(sshProvider, availableConnections));
                  }}
                />
                <span className="block h-full rounded-xl border border-gray-200 p-4 peer-checked:border-crimson peer-checked:bg-crimson/5 peer-focus-visible:ring-2 peer-focus-visible:ring-crimson dark:border-white/15 dark:peer-checked:border-crimson-light">
                  <span className="block text-sm font-medium">{item.label}</span>
                  <span className="mt-1 block text-xs text-gray-500 dark:text-gray-400">
                    {item.id === 'existing_ssh'
                      ? 'Connect a machine you already have'
                      : item.modes.includes('provision')
                        ? 'Create or connect a cloud server'
                        : 'Connect a cloud server'}
                  </span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
        {provider && provider.modes.length > 1 && (
          <fieldset className="flex flex-wrap gap-4" aria-label="Cloud resource">
            {provider.modes.map((item) => (
              <label key={item} className="flex items-center gap-2 text-sm">
                <input
                  type="radio"
                  name="mode"
                  value={item}
                  checked={mode === item}
                  onChange={() => {
                    setMode(item);
                    setObserveOnly(false);
                    setAdvancedOpen(false);
                  }}
                />
                {item === 'attach' ? 'Connect existing' : 'Create new'}
              </label>
            ))}
          </fieldset>
        )}
        <div>
          <Field label="Server name">
            <input
              className={inputClass}
              required
              value={name}
              placeholder="e.g. Research server"
              aria-describedby="server-name-help"
              onChange={(event) => setName(event.target.value)}
            />
          </Field>
          <span id="server-name-help" className="block text-xs text-gray-500 dark:text-gray-400">
            Name shown in your server list.
          </span>
        </div>
        {provider && cloud && (
          <CredentialPicker
            key={provider.id}
            provider={provider}
            connections={availableConnections}
            value={account}
            onChange={setAccount}
            serverName={name}
            error={accountError}
          />
        )}
        {resourceFields.length > 0 && (
          <Section title={cloud ? 'Cloud resource' : 'Server address'}>
            <div
              className={resourceFields.length === 2 ? 'grid gap-4 sm:grid-cols-2' : 'space-y-4'}
            >
              <ProviderFields
                fields={resourceFields}
                values={values}
                onChange={setValues}
                errors={fieldErrors}
              />
            </div>
          </Section>
        )}
        {templateFields.length > 0 && (
          <Section title="Machine template">
            <div className="grid gap-4 sm:grid-cols-2">
              <ProviderFields
                fields={templateFields}
                values={values}
                onChange={setValues}
                errors={fieldErrors}
              />
            </div>
          </Section>
        )}
        {!observeOnly && (
          <Section title="Sandbox storage">
            <div>
              <Field label="Sandbox storage path">
                <input
                  className={inputClass}
                  required
                  maxLength={4096}
                  value={sandboxStoragePath}
                  placeholder="e.g. /mnt/data"
                  autoComplete="off"
                  spellCheck={false}
                  aria-invalid={Boolean(fieldErrors.sandbox_storage_path) || undefined}
                  aria-describedby={[
                    `${storageId}-help`,
                    fieldErrors.sandbox_storage_path && `${storageId}-error`,
                  ]
                    .filter(Boolean)
                    .join(' ')}
                  onChange={(event) => setSandboxStorageInput(event.target.value)}
                />
              </Field>
              <p id={`${storageId}-help`} className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                Absolute path to an existing mounted data disk on this server. Sandboxes,
                workspaces, and session data are stored there.
                {providerId === 'cloudlab' &&
                  mode === 'provision' &&
                  ` CloudLab's small-lan profile mounts its data disk at ${CLOUDLAB_DATA_MOUNT}; change this if your template mounts it elsewhere.`}
              </p>
              {fieldErrors.sandbox_storage_path && (
                <p
                  id={`${storageId}-error`}
                  role="alert"
                  className="mt-1 text-xs text-red-700 dark:text-red-300"
                >
                  {fieldErrors.sandbox_storage_path}
                </p>
              )}
            </div>
          </Section>
        )}
        {!observeOnly && provider && (!cloud || needsSshKey || accessFields.length > 0) && (
          <Section title="SSH access">
            <div
              className={
                accessFields.length === 2 &&
                accessFields.every((field) => field.type !== 'textarea' && field.type !== 'json')
                  ? 'grid gap-4 sm:grid-cols-2'
                  : 'space-y-4'
              }
            >
              <ProviderFields
                fields={accessFields}
                values={values}
                onChange={setValues}
                errors={fieldErrors}
              />
            </div>
            {needsSshKey && deploymentSshProvider ? (
              <CredentialPicker
                provider={deploymentSshProvider}
                connections={availableConnections}
                value={ssh}
                onChange={setSsh}
                suppressUnverified={supportsUncheckedCloudLabSsh}
                serverName={name}
                error={sshError}
              />
            ) : !cloud ? (
              <CredentialPicker
                provider={provider}
                connections={availableConnections}
                value={account}
                onChange={setAccount}
                serverName={name}
                error={accountError}
              />
            ) : null}
            {skipsCloudLabHostKeyCheck && (
              <p className="text-sm text-amber-700 dark:text-amber-300">
                CloudLab SSH host identity will not be verified.
              </p>
            )}
          </Section>
        )}
        {(advancedFields.length > 0 || canObserve) && (
          <details
            open={advancedOpen}
            onToggle={(event) => setAdvancedOpen(event.currentTarget.open)}
          >
            <summary className="cursor-pointer text-sm text-gray-600 dark:text-gray-300">
              Advanced options
            </summary>
            <div className="mt-3 space-y-4">
              <ProviderFields
                fields={advancedFields}
                values={values}
                onChange={setValues}
                errors={fieldErrors}
              />
              {canObserve && (
                <label className="flex items-start gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={observeOnly}
                    onChange={(event) => setObserveOnly(event.target.checked)}
                  />
                  Only track this resource (skip server setup)
                </label>
              )}
            </div>
          </details>
        )}
        {!deploymentConfigured && !observeOnly && (
          <div
            role="alert"
            className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-500/25 dark:bg-amber-500/10 dark:text-amber-200"
          >
            <p>No server setup is configured. Configure it before this server can run sandboxes.</p>
            {onConfigureSetup && (
              <button
                ref={configureSetupButtonRef}
                className={`${buttonClass} mt-3 bg-white dark:bg-gray-900`}
                type="button"
                onClick={onConfigureSetup}
              >
                Configure server setup
              </button>
            )}
          </div>
        )}
        <div className="flex flex-wrap items-center justify-between gap-4 border-t border-gray-200 pt-4 dark:border-white/10">
          <p className="text-xs text-gray-500 dark:text-gray-400">
            {observeOnly
              ? 'Tracks status only. This resource cannot run sandboxes.'
              : 'Access checks and setup run automatically.'}
          </p>
          <button
            className={primaryClass}
            type="submit"
            disabled={!provider || (!observeOnly && !deploymentConfigured)}
          >
            {busy
              ? 'Submitting…'
              : observeOnly
                ? 'Import resource'
                : mode === 'provision'
                  ? 'Create server'
                  : 'Add server'}
          </button>
        </div>
      </fieldset>
      <ErrorNotice error={error} />
    </form>
  );
}
