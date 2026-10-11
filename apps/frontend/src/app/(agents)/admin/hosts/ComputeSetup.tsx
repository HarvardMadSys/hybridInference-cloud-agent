'use client';

import { useEffect, useRef, useState } from 'react';

import { config } from '@/config/env';
import {
  createAutomaticComputeSetup,
  getComputeAutomaticSetup,
  saveComputeDeploymentProfile,
  type AutomaticComputeSetupRequest,
  type AutomaticComputeSetupStatus,
  type ComputeConnection,
} from '@/lib/api/compute';
import { getErrorMessage } from '@/lib/utils/errors';

import { buttonClass, ErrorNotice, inputClass, primaryClass } from './compute-ui';

const CREDENTIALS = [
  ['gateway.key', 'Inference tunnel private key'],
  ['gateway.known_hosts', 'Inference tunnel verified hosts'],
  ['mcp.key', 'MCP tunnel private key'],
  ['mcp.known_hosts', 'MCP tunnel verified hosts'],
  ['control.key', 'Control tunnel private key'],
  ['control.known_hosts', 'Control tunnel verified hosts'],
] as const;

type AutomaticSetupParameters = Omit<AutomaticComputeSetupRequest, 'request_id'>;
type SavedAutomaticSetup = {
  deployment_sha: string;
  request_id: string;
  parameters: AutomaticSetupParameters;
};

function automaticSetupStorageKey() {
  return `cloud-agent:automatic-server-setup:${config.apiBase}:${window.location.pathname}`;
}

function setupSignature(parameters: AutomaticSetupParameters) {
  const target = (machine: AutomaticSetupParameters['gateway']) => [
    machine.connection_id,
    machine.address,
    machine.port,
    machine.username,
  ];
  return JSON.stringify([
    target(parameters.gateway),
    target(parameters.control),
    parameters.gateway_port,
    parameters.chat_network_pool,
    parameters.dns_server,
    parameters.protected_cidrs,
    parameters.slots,
  ]);
}

function isSetupParameters(value: unknown): value is AutomaticSetupParameters {
  if (!value || typeof value !== 'object') return false;
  const parameters = value as Record<string, unknown>;
  const isMachine = (machine: unknown) => {
    if (!machine || typeof machine !== 'object') return false;
    const fields = machine as Record<string, unknown>;
    return (
      ['connection_id', 'address', 'username'].every(
        (field) => typeof fields[field] === 'string',
      ) && Number.isInteger(fields.port)
    );
  };
  return (
    isMachine(parameters.gateway) &&
    isMachine(parameters.control) &&
    Number.isInteger(parameters.gateway_port) &&
    typeof parameters.chat_network_pool === 'string' &&
    typeof parameters.dns_server === 'string' &&
    Array.isArray(parameters.protected_cidrs) &&
    parameters.protected_cidrs.every((cidr) => typeof cidr === 'string') &&
    Number.isInteger(parameters.slots)
  );
}

function readSavedSetup(deploymentSha: string | null): SavedAutomaticSetup | null {
  try {
    const stored: unknown = JSON.parse(
      sessionStorage.getItem(automaticSetupStorageKey()) ?? 'null',
    );
    if (!stored || typeof stored !== 'object') return null;
    const saved = stored as Record<string, unknown>;
    if (
      deploymentSha &&
      saved.deployment_sha === deploymentSha &&
      typeof saved.request_id === 'string' &&
      saved.request_id &&
      isSetupParameters(saved.parameters)
    ) {
      return saved as SavedAutomaticSetup;
    }
  } catch {
    // Server-side saved settings still support recovery when browser storage is unavailable.
  }
  return null;
}

function saveSetupIntent(saved: SavedAutomaticSetup | null) {
  try {
    if (saved) sessionStorage.setItem(automaticSetupStorageKey(), JSON.stringify(saved));
    else sessionStorage.removeItem(automaticSetupStorageKey());
  } catch {
    // A storage restriction must not block an administrator's setup request.
  }
}

async function encode(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = '';
  const size = 0x8000;
  for (let offset = 0; offset < bytes.length; offset += size) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + size));
  }
  return btoa(binary);
}

export default function ComputeSetup({
  connections,
  onSaved,
}: {
  connections: ComputeConnection[];
  onSaved: () => void;
}) {
  const [manual, setManual] = useState(false);
  const [busy, setBusy] = useState(false);

  return (
    <div className="space-y-5">
      <div className="flex gap-2" aria-label="Server setup method">
        <button
          className={manual ? buttonClass : primaryClass}
          type="button"
          disabled={busy}
          aria-pressed={!manual}
          onClick={() => setManual(false)}
        >
          Automatic setup
        </button>
        <button
          className={manual ? primaryClass : buttonClass}
          type="button"
          disabled={busy}
          aria-pressed={manual}
          onClick={() => setManual(true)}
        >
          Advanced manual setup
        </button>
      </div>
      <div hidden={manual}>
        <AutomaticComputeSetup connections={connections} onSaved={onSaved} onBusy={setBusy} />
      </div>
      {manual && <ManualComputeSetup onSaved={onSaved} />}
    </div>
  );
}

function AutomaticComputeSetup({
  connections,
  onSaved,
  onBusy,
}: {
  connections: ComputeConnection[];
  onSaved: () => void;
  onBusy: (busy: boolean) => void;
}) {
  const sshConnections = connections.filter(
    (connection) => connection.provider_id === 'existing_ssh',
  );
  const [status, setStatus] = useState<AutomaticComputeSetupStatus | null>(null);
  const [gateway, setGateway] = useState({
    connection_id: '',
    address: '',
    port: '22',
    username: '',
  });
  const [control, setControl] = useState({
    connection_id: '',
    address: '',
    port: '22',
    username: '',
  });
  const [gatewayPort, setGatewayPort] = useState('8080');
  const [networkPool, setNetworkPool] = useState('');
  const [dnsServer, setDnsServer] = useState('');
  const [protectedCidrs, setProtectedCidrs] = useState('');
  const [slots, setSlots] = useState('2');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const intent = useRef<{ signature: string; requestId: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    void getComputeAutomaticSetup(controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return;
        const saved = readSavedSetup(result.deployment_sha);
        const parameters =
          result.pending_request_id && result.defaults
            ? result.defaults
            : (saved?.parameters ?? result.defaults);
        const requestId =
          result.pending_request_id && result.defaults
            ? result.pending_request_id
            : saved?.request_id;
        if (parameters) {
          setGateway({ ...parameters.gateway, port: String(parameters.gateway.port) });
          setControl({ ...parameters.control, port: String(parameters.control.port) });
          setGatewayPort(String(parameters.gateway_port));
          setNetworkPool(parameters.chat_network_pool);
          setDnsServer(parameters.dns_server);
          setProtectedCidrs(parameters.protected_cidrs.join('\n'));
          setSlots(String(parameters.slots));
          if (requestId) {
            intent.current = { signature: setupSignature(parameters), requestId };
            setPending(true);
          }
        }
        setStatus(result);
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(getErrorMessage(reason));
      });
    return () => controller.abort();
  }, []);

  async function submit() {
    if (busy || !status?.available) return;
    setBusy(true);
    onBusy(true);
    setError(null);
    try {
      const parameters = {
        gateway: {
          connection_id: gateway.connection_id,
          address: gateway.address.trim(),
          port: Number(gateway.port),
          username: gateway.username.trim(),
        },
        control: {
          connection_id: control.connection_id,
          address: control.address.trim(),
          port: Number(control.port),
          username: control.username.trim(),
        },
        gateway_port: Number(gatewayPort),
        chat_network_pool: networkPool.trim(),
        dns_server: dnsServer.trim(),
        protected_cidrs: protectedCidrs.split(/[\s,]+/).filter(Boolean),
        slots: Number(slots),
      };
      const signature = setupSignature(parameters);
      if (intent.current?.signature !== signature) {
        intent.current = { signature, requestId: crypto.randomUUID() };
      }
      if (status.deployment_sha) {
        saveSetupIntent({
          deployment_sha: status.deployment_sha,
          request_id: intent.current.requestId,
          parameters,
        });
      }
      setPending(true);
      await createAutomaticComputeSetup({ ...parameters, request_id: intent.current.requestId });
      saveSetupIntent(null);
      intent.current = null;
      setPending(false);
      onSaved();
    } catch (reason) {
      setError(getErrorMessage(reason));
    } finally {
      setBusy(false);
      onBusy(false);
    }
  }

  return (
    <form
      className="space-y-5"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <p className="text-sm text-gray-600 dark:text-gray-300">
        Select your gateway and control-plane machines and confirm the sandbox network. Cloud Agent
        uses its deployed release, creates the Host environment and dedicated tunnel credentials,
        and configures the connections automatically.
      </p>
      {!status && !error && <p role="status">Checking automatic setup availability…</p>}
      {status && !status.available && (
        <div role="alert" className="space-y-2 text-sm text-amber-700 dark:text-amber-300">
          <p>Automatic setup is unavailable for this control-plane deployment.</p>
          {status.blocked_reasons.map((reason) => (
            <p key={reason.code}>{reason.message}</p>
          ))}
        </div>
      )}
      {sshConnections.length === 0 && (
        <p className="text-sm text-gray-600 dark:text-gray-300">
          Add SSH connections for the gateway and control-plane machines under Connections first.
        </p>
      )}
      <p className="text-sm text-gray-600 dark:text-gray-300">
        Use SSH accounts with passwordless sudo on the stable machines. The gateway serves
        inference; the control plane runs the main API and MCP listener.
      </p>
      <fieldset className="space-y-4" disabled={busy || !status?.available}>
        <div className="grid gap-4 sm:grid-cols-2">
          {(
            [
              ['Gateway', gateway, setGateway],
              ['Control-plane', control, setControl],
            ] as const
          ).map(([name, value, update]) => (
            <fieldset className="space-y-3" key={name}>
              <legend className="mb-2 text-sm font-semibold">{name} machine</legend>
              <label className="block text-sm">
                <span className="mb-1 block font-medium">{name} host address</span>
                <input
                  className={inputClass}
                  required
                  value={value.address}
                  onChange={(event) => update({ ...value, address: event.target.value })}
                />
              </label>
              <label className="block text-sm">
                <span className="mb-1 block font-medium">{name} SSH username</span>
                <input
                  className={inputClass}
                  required
                  value={value.username}
                  onChange={(event) => update({ ...value, username: event.target.value })}
                />
              </label>
              <label className="block text-sm">
                <span className="mb-1 block font-medium">{name} SSH connection</span>
                <select
                  className={inputClass}
                  required
                  value={value.connection_id}
                  onChange={(event) => update({ ...value, connection_id: event.target.value })}
                >
                  <option value="">Select an administrative SSH key</option>
                  {sshConnections.map((connection) => (
                    <option key={connection.id} value={connection.id}>
                      {connection.name}
                    </option>
                  ))}
                </select>
              </label>
              <details>
                <summary className="cursor-pointer text-sm">Custom SSH port</summary>
                <label className="mt-3 block text-sm">
                  <span className="mb-1 block font-medium">{name} SSH port</span>
                  <input
                    className={inputClass}
                    type="number"
                    min={1}
                    max={65535}
                    required
                    value={value.port}
                    onChange={(event) => update({ ...value, port: event.target.value })}
                  />
                </label>
              </details>
            </fieldset>
          ))}
        </div>
        <details>
          <summary className="cursor-pointer text-sm">Custom inference port</summary>
          <label className="mt-3 block text-sm">
            <span className="mb-1 block font-medium">Gateway inference port</span>
            <input
              className={inputClass}
              type="number"
              min={1}
              max={65535}
              required
              value={gatewayPort}
              onChange={(event) => setGatewayPort(event.target.value)}
            />
          </label>
        </details>
        <div className="grid gap-4 sm:grid-cols-2">
          <label className="text-sm">
            <span className="mb-1 block font-medium">Server setup capacity</span>
            <input
              className={inputClass}
              aria-label="Server setup capacity"
              type="number"
              min={1}
              max={8}
              required
              value={slots}
              onChange={(event) => setSlots(event.target.value)}
            />
            <span className="mt-1 block text-gray-600 dark:text-gray-300">
              Two setups support one server plus a spare for replacement.
            </span>
          </label>
          <label className="text-sm">
            <span className="mb-1 block font-medium">Sandbox network pool</span>
            <input
              className={inputClass}
              aria-label="Sandbox network pool"
              required
              value={networkPool}
              placeholder="IPv4 CIDR approved for sandbox bridges"
              onChange={(event) => setNetworkPool(event.target.value)}
            />
            <span className="mt-1 block text-gray-600 dark:text-gray-300">
              Choose a range that does not overlap your host or site networks.
            </span>
          </label>
          <label className="text-sm">
            <span className="mb-1 block font-medium">Sandbox DNS server</span>
            <input
              className={inputClass}
              required
              value={dnsServer}
              placeholder="Approved IPv4 resolver"
              onChange={(event) => setDnsServer(event.target.value)}
            />
          </label>
        </div>
        <label className="block text-sm">
          <span className="mb-1 block font-medium">Protected infrastructure networks</span>
          <textarea
            className={inputClass}
            aria-label="Protected infrastructure networks"
            rows={3}
            required
            value={protectedCidrs}
            placeholder="IPv4 CIDRs, separated by commas or newlines"
            onChange={(event) => setProtectedCidrs(event.target.value)}
          />
          <span className="mt-1 block text-gray-600 dark:text-gray-300">
            Include infrastructure networks that sandboxes must not reach.
          </span>
        </label>
      </fieldset>
      {error && <ErrorNotice error={error} />}
      {pending && (
        <div className="space-y-2 text-sm text-gray-600 dark:text-gray-300">
          <p>Resume the previous setup with these settings, or start a new setup.</p>
          <button
            className={buttonClass}
            disabled={busy}
            type="button"
            onClick={() => {
              intent.current = null;
              setPending(false);
              setError(null);
              saveSetupIntent(null);
            }}
          >
            Start new setup
          </button>
          <p>Any unfinished setup remains encrypted and unavailable for servers.</p>
        </div>
      )}
      <div className="flex justify-end border-t border-gray-200 pt-4 dark:border-white/10">
        <button
          className={primaryClass}
          disabled={busy || !status?.available || sshConnections.length === 0}
          type="submit"
        >
          {busy
            ? 'Configuring server setup…'
            : pending
              ? 'Retry automatic setup'
              : 'Configure automatically'}
        </button>
      </div>
    </form>
  );
}

function ManualComputeSetup({ onSaved }: { onSaved: () => void }) {
  const [id, setId] = useState('');
  const [label, setLabel] = useState('');
  const [sha, setSha] = useState('');
  const [root, setRoot] = useState('/opt/cloud-agent');
  const [environment, setEnvironment] = useState('');
  const [bundle, setBundle] = useState<File | null>(null);
  const [files, setFiles] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit() {
    if (!bundle || busy) return;
    setBusy(true);
    setError(null);
    try {
      await saveComputeDeploymentProfile({
        id,
        label,
        deployment_sha: sha,
        deploy_root: root,
        environment,
        files: Object.fromEntries(
          CREDENTIALS.map(([name]) => [`.compute-secrets/${name}`, files[name] ?? '']),
        ),
        source_bundle_base64: await encode(bundle),
        install_prerequisites: true,
        timeout_seconds: 3600,
      });
      onSaved();
    } catch (reason) {
      setError(getErrorMessage(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      className="space-y-5"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
    >
      <p className="text-sm text-gray-600 dark:text-gray-300">
        Register the reviewed release and the credentials used to install an isolated Host. Secrets
        are encrypted before storage and are never returned by the API.
      </p>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="text-sm">
          <span className="mb-1 block font-medium">Profile ID</span>
          <input
            className={inputClass}
            value={id}
            pattern="[a-z][a-z0-9_-]{0,63}"
            required
            placeholder="primary-kata"
            onChange={(event) => setId(event.target.value)}
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block font-medium">Display name</span>
          <input
            className={inputClass}
            value={label}
            required
            placeholder="Primary Kata host"
            onChange={(event) => setLabel(event.target.value)}
          />
        </label>
        <label className="text-sm sm:col-span-2">
          <span className="mb-1 block font-medium">Reviewed release revision</span>
          <input
            className={inputClass}
            value={sha}
            pattern="[a-f0-9]{40}"
            required
            placeholder="40-character Git commit"
            onChange={(event) => setSha(event.target.value)}
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block font-medium">Remote install directory</span>
          <input
            className={inputClass}
            value={root}
            required
            onChange={(event) => setRoot(event.target.value)}
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block font-medium">Release bundle</span>
          <input
            className={inputClass}
            type="file"
            accept=".tar,.tar.gz,.tgz,application/gzip"
            required
            onChange={(event) => setBundle(event.target.files?.[0] ?? null)}
          />
        </label>
      </div>
      <label className="block text-sm">
        <span className="mb-1 block font-medium">Host environment</span>
        <textarea
          className={inputClass}
          rows={14}
          value={environment}
          required
          spellCheck={false}
          placeholder="Paste the completed compute-host.env template"
          onChange={(event) => setEnvironment(event.target.value)}
        />
      </label>
      <details>
        <summary className="cursor-pointer text-sm font-medium">Tunnel credentials</summary>
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          {CREDENTIALS.map(([name, title]) => (
            <label className="text-sm" key={name}>
              <span className="mb-1 block font-medium">{title}</span>
              <textarea
                className={inputClass}
                rows={name.endsWith('.key') ? 7 : 4}
                required
                spellCheck={false}
                value={files[name] ?? ''}
                onChange={(event) =>
                  setFiles((current) => ({ ...current, [name]: event.target.value }))
                }
              />
            </label>
          ))}
        </div>
      </details>
      {error && <ErrorNotice error={error} />}
      <div className="flex justify-end border-t border-gray-200 pt-4 dark:border-white/10">
        <button className={primaryClass} disabled={busy || !bundle} type="submit">
          {busy ? 'Encrypting and saving…' : 'Save server setup'}
        </button>
      </div>
    </form>
  );
}
