import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

export interface PromptClarificationAnswer {
  question: string;
  answer: string;
}

export interface RewritePromptRequest {
  prompt: string;
  model: string;
  clarify: boolean;
  answers?: PromptClarificationAnswer[];
}

export type RewritePromptResult =
  | { kind: 'questions'; questions: string[] }
  | { kind: 'prompt'; prompt: string };

/** Improve a draft without creating a job or sending a conversation message. */
export async function rewritePrompt(
  body: RewritePromptRequest,
  signal?: AbortSignal,
): Promise<RewritePromptResult> {
  const response = await fetchWithAuth(config.apiBase, '/v1/agent/prompts/rewrite', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  return jsonOrThrow<RewritePromptResult>(response);
}
