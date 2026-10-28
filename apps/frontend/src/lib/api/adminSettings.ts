import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow, safeFetch } from './client';

const SETTINGS = '/admin/agent/settings';

export type AdminSettingValue = string | number | boolean;

export type DeploymentManagement = 'settings' | 'specialized' | 'rotation' | 'deployment';
export type DeploymentRequirement = 'required' | 'optional' | 'conditional';
export type DeploymentState =
  | 'configured'
  | 'missing'
  | 'weak'
  | 'fallback'
  | 'not_used'
  | 'unknown';

/** A fixed, application-owned destination; never a backend-provided URL. */
export interface DeploymentSetupAction {
  target: string;
  label: string;
}

export interface DeploymentSetupDefinition {
  /** Stable semantic ID. The legacy key remains the DOM anchor. */
  id?: string;
  key: string;
  label: string;
  description: string;
  management?: DeploymentManagement;
  requirement?: DeploymentRequirement;
  state?: DeploymentState;
  actions?: DeploymentSetupAction[];
}

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
}

export interface AgentAdminSettings {
  revision: number;
  definitions: AdminSettingDefinition[];
  values: Record<string, AdminSettingValue | null>;
  secret_configured: Record<string, boolean>;
  /** Deployment configuration and its application-owned management entry points. */
  bootstrap: DeploymentSetupDefinition[];
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
