import { agentApiBase } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

export interface DatabaseTools {
  enabled: boolean;
  available: boolean;
  url: string | null;
  setup_required?: boolean;
}

export async function setupPgAdmin(
  email: string,
  password: string,
  signal?: AbortSignal,
): Promise<DatabaseTools> {
  return jsonOrThrow<DatabaseTools>(
    await fetchWithAuth(agentApiBase, '/admin/agent/database-tools/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
      signal,
    }),
  );
}

export async function getDatabaseTools(signal?: AbortSignal): Promise<DatabaseTools> {
  return jsonOrThrow<DatabaseTools>(
    await fetchWithAuth(agentApiBase, '/admin/agent/database-tools', { signal }),
  );
}
