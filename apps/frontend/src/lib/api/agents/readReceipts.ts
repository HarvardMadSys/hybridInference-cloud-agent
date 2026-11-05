import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from '../client';

export interface AgentReadReceiptApi {
  thread_id: string;
  job_id: string;
  turn_no: number;
  attempt_id: number | null;
}
export interface AgentReadReceiptsResponse {
  user_id: string;
  receipts: AgentReadReceiptApi[];
  next_cursor: string | null;
  sync_cursor: string | null;
}
const PATH = '/v1/agent/conversations/read-receipts';

export async function listAgentReadReceipts(
  cursor?: string,
  signal?: AbortSignal,
): Promise<AgentReadReceiptsResponse> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
  return jsonOrThrow(
    await fetchWithAuth(config.apiBase, PATH + query, { signal, cache: 'no-store' }),
  );
}

export async function saveAgentReadReceipts(
  receipts: Pick<AgentReadReceiptApi, 'job_id' | 'attempt_id'>[],
  signal?: AbortSignal,
): Promise<AgentReadReceiptsResponse> {
  return jsonOrThrow(
    await fetchWithAuth(config.apiBase, PATH, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ receipts }),
      signal,
    }),
  );
}
