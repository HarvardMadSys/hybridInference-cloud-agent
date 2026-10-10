import { fetchWithAuth, jsonOrThrow } from './client';
import { config } from '@/config/env';

export interface AgentQuestionApi {
  id: string;
  header: string;
  question: string;
  multiple: boolean;
  options: { label: string; description: string }[];
  allow_custom: boolean;
}

export interface AgentQuestionRequestApi {
  request_id: string;
  questions: AgentQuestionApi[];
}

/** The live turn that owns these requests, including when no question is waiting. */
export interface AgentQuestionsApi {
  questions: AgentQuestionRequestApi[];
  job_id: string | null;
  attempt_id: number | null;
}

export interface AgentQuestionAnswerApi {
  request_id: string;
  job_id: string;
  attempt_id: number;
  answers: Record<string, string[]>;
  reject?: boolean;
}

function questionsPath(threadId: string): string {
  return `/v1/agent/threads/${encodeURIComponent(threadId)}/questions`;
}

export async function listThreadQuestions(
  threadId: string,
  signal?: AbortSignal,
): Promise<AgentQuestionsApi> {
  const response = await fetchWithAuth(config.apiBase, questionsPath(threadId), { signal });
  return jsonOrThrow<AgentQuestionsApi>(response);
}

export async function answerThreadQuestion(
  threadId: string,
  answer: AgentQuestionAnswerApi,
): Promise<{ accepted: boolean; reason: string | null }> {
  const response = await fetchWithAuth(config.apiBase, questionsPath(threadId), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(answer),
  });
  return jsonOrThrow(response);
}
