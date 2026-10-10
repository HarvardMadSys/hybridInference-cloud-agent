import { config } from '@/config/env';

import { fetchWithAuth, jsonOrThrow } from './client';

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
  /** Secrets are write-only; their values are always null in a snapshot. */
  secret: boolean;
}

export interface AgentAdminSettings {
  revision: number;
  definitions: AdminSettingDefinition[];
  values: Record<string, AdminSettingValue | null>;
  secret_configured: Record<string, boolean>;
  /** Deployment prerequisites that must be supplied before the service starts. */
  bootstrap: { key: string; label: string; description: string }[];
}

export async function getAgentAdminSettings(signal?: AbortSignal): Promise<AgentAdminSettings> {
  return jsonOrThrow<AgentAdminSettings>(
    await fetchWithAuth(config.apiBase, SETTINGS, { signal, cache: 'no-store' }),
  );
}

/** Patch only the named settings. Null restores the built-in default. */
export async function setAgentAdminSettings(
  settings: Record<string, AdminSettingValue | null>,
  signal?: AbortSignal,
): Promise<AgentAdminSettings> {
  return jsonOrThrow<AgentAdminSettings>(
    await fetchWithAuth(config.apiBase, SETTINGS, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ settings }),
      signal,
    }),
  );
}
