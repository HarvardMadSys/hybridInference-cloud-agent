import { agentApiBase } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

export interface DatabaseTools {
  enabled: boolean;
  available: boolean;
  url: string | null;
}

export async function getDatabaseTools(signal?: AbortSignal): Promise<DatabaseTools> {
  return jsonOrThrow<DatabaseTools>(
    await fetchWithAuth(agentApiBase, '/admin/agent/database-tools', { signal }),
  );
}
