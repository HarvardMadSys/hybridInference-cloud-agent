import type { AgentThreadMessageApi } from '@/lib/api/agents';

/** Carry the API's explicit preview boundary into both current and stored turns. */
export function mapThreadMessage(message: AgentThreadMessageApi) {
  return {
    id: message.id,
    role: message.role,
    content: message.content,
    jobId: message.job_id,
    createdAt: message.created_at,
    ...(message.mid_turn ? { midTurn: true } : {}),
    ...(message.content_truncated
      ? { contentTruncated: true, nextOffset: message.next_offset }
      : {}),
  };
}
