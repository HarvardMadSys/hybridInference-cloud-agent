import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow, safeFetch } from './client';

const SETTINGS = '/admin/agent/settings';

export type AdminSettingValue = string | number | boolean;

export interface AdminSettingDefinition {
  key: string;
  label: string;
  description: string;
  category: string;
  kind: 'string' | 'boolean' | 'integer' | 'number';
  default: AdminSettingValue;
  minimum?: number;
  maximum?: number;
  choices?: string[];
  minimum_bytes?: number;
  /** Pinned identity and existing credential keys need a separate migration. */
  read_only?: boolean;
  /** Secrets are write-only; their values are always null in a snapshot. */
  secret: boolean;
  /** When a saved value takes effect; `unverified` makes no promise. */
  apply_mode?: 'next_operation' | 'new_session' | 'restart' | 'host_reenroll' | 'unverified';
  apply_description?: string;
}

export interface AgentAdminSettings {
  revision: number;
  definitions: AdminSettingDefinition[];
  values: Record<string, AdminSettingValue | null>;
  secret_configured: Record<string, boolean>;
  /**
   * Deployment-managed configuration notes. The API still returns them, with
   * optional metadata, but the settings pages do not render them.
   */
  bootstrap: { id?: string; key: string; label: string; description: string }[];
  setup?: {
    required: boolean;
    groups: {
      key: string;
      label: string;
      required: boolean;
      configured: boolean;
      needs_setup: boolean;
      missing: { key: string; label: string }[];
    }[];
    bootstrap_missing: { key: string; label: string }[];
  };
}

async function settingsRequest(init: RequestInit, recoveryToken?: string): Promise<Response> {
  if (recoveryToken) {
    return safeFetch(`${config.apiBase}${SETTINGS}`, {
      ...init,
      headers: { ...init.headers, Authorization: `Bearer ${recoveryToken}` },
      credentials: 'omit',
      cache: 'no-store',
    });
  }
  return fetchWithAuth(config.apiBase, SETTINGS, init);
}

export async function getAgentAdminSettings(
  signal?: AbortSignal,
  recoveryToken?: string,
): Promise<AgentAdminSettings> {
  return jsonOrThrow<AgentAdminSettings>(
    await settingsRequest({ signal, cache: 'no-store' }, recoveryToken),
  );
}

/**
 * Patch only the named settings. Null restores the built-in default.
 *
 * Refused with a 409 unless the settings are still at `expectedRevision`, the
 * revision the change was reviewed against.
 */
export async function setAgentAdminSettings(
  settings: Record<string, AdminSettingValue | null>,
  expectedRevision: number,
  signal?: AbortSignal,
  recoveryToken?: string,
): Promise<AgentAdminSettings> {
  return jsonOrThrow<AgentAdminSettings>(
    await settingsRequest(
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ settings, expected_revision: expectedRevision }),
        signal,
      },
      recoveryToken,
    ),
  );
}
