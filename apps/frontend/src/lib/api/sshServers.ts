import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

const SSH = '/v1/agent/ssh-servers';

/** A host key by its kind and the fingerprint `ssh-keygen -l` prints. */
export interface SshHostKeyApi {
  type: string;
  fingerprint: string;
}

/**
 * One of the owner's servers. The platform holds its private key; the page
 * only ever sees the public key and the command that installs it.
 */
export interface SshServerApi {
  id: string;
  name: string;
  description: string;
  address: string;
  port: number;
  username: string;
  /** `ready` once a check confirmed its host key and logged in. */
  status: 'pending' | 'ready';
  pinned_address: string | null;
  host_key: SshHostKeyApi | null;
  /** What the last check found, waiting for the owner to confirm. */
  scan: { address: string; host_keys: SshHostKeyApi[] } | null;
  public_key: string;
  install_command: string;
  /** A new key waiting for its first successful check. */
  pending_public_key: string | null;
  pending_install_command: string | null;
  last_check_at: string | null;
  last_check_error: string | null;
  last_check_message: string | null;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface SshServerListApi {
  /** False when the deployment cannot store keys, so nothing can be added. */
  configured: boolean;
  servers: SshServerApi[];
}

export interface SshServerInput {
  name: string;
  address: string;
  port: number;
  username: string;
  description: string;
}

/** One server in a chat's selection. */
export interface SshBindingApi {
  server_id: string;
  name: string;
  status: 'pending' | 'ready';
  enabled: boolean;
}

function json(method: string, body: unknown): RequestInit {
  return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) };
}

function server(id: string, suffix = ''): string {
  return `${SSH}/${encodeURIComponent(id)}${suffix}`;
}

export async function listSshServers(): Promise<SshServerListApi> {
  const response = await fetchWithAuth(config.apiBase, SSH, { cache: 'no-store' });
  return jsonOrThrow<SshServerListApi>(response);
}

export async function createSshServer(input: SshServerInput): Promise<SshServerApi> {
  const response = await fetchWithAuth(config.apiBase, SSH, json('POST', input));
  return jsonOrThrow<SshServerApi>(response);
}

export async function updateSshServer(
  current: SshServerApi,
  changes: Partial<SshServerInput>,
): Promise<SshServerApi> {
  const response = await fetchWithAuth(
    config.apiBase,
    server(current.id),
    json('PATCH', { revision: current.revision, ...changes }),
  );
  return jsonOrThrow<SshServerApi>(response);
}

/** Resolve the address and read the host keys for the owner to confirm. */
export async function scanSshServer(current: SshServerApi): Promise<SshServerApi> {
  const response = await fetchWithAuth(
    config.apiBase,
    server(current.id, '/scan'),
    json('POST', { revision: current.revision }),
  );
  return jsonOrThrow<SshServerApi>(response);
}

/** Log in once against the confirmed host key; a pending new key replaces the old on success. */
export async function verifySshServer(
  current: SshServerApi,
  fingerprint: string,
): Promise<SshServerApi> {
  const response = await fetchWithAuth(
    config.apiBase,
    server(current.id, '/verify'),
    json('POST', { revision: current.revision, fingerprint }),
  );
  return jsonOrThrow<SshServerApi>(response);
}

export async function rotateSshServerKey(current: SshServerApi): Promise<SshServerApi> {
  const response = await fetchWithAuth(
    config.apiBase,
    server(current.id, '/rotate-key'),
    json('POST', { revision: current.revision }),
  );
  return jsonOrThrow<SshServerApi>(response);
}

export async function deleteSshServer(current: SshServerApi): Promise<void> {
  const response = await fetchWithAuth(config.apiBase, server(current.id), { method: 'DELETE' });
  if (!response.ok) await jsonOrThrow<never>(response);
}

export async function getSshThreadBindings(threadId: string): Promise<SshBindingApi[]> {
  const response = await fetchWithAuth(
    config.apiBase,
    `${SSH}/threads/${encodeURIComponent(threadId)}/bindings`,
    { cache: 'no-store' },
  );
  return (await jsonOrThrow<{ bindings: SshBindingApi[] }>(response)).bindings;
}

/** Choose exactly the servers a chat can reach; effective at once. */
export async function replaceSshThreadBindings(
  threadId: string,
  serverIds: string[],
): Promise<SshBindingApi[]> {
  const response = await fetchWithAuth(
    config.apiBase,
    `${SSH}/threads/${encodeURIComponent(threadId)}/bindings`,
    json('PUT', { server_ids: serverIds }),
  );
  return (await jsonOrThrow<{ bindings: SshBindingApi[] }>(response)).bindings;
}
