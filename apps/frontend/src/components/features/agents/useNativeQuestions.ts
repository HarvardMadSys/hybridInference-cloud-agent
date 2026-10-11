'use client';

import { useEffect, useRef, useState } from 'react';
import {
  answerThreadQuestion,
  listThreadQuestions,
  type AgentQuestionRequestApi,
  type AgentQuestionsApi,
} from '@/lib/api/questions';
import { getErrorMessage } from '@/lib/utils/errors';

export const QUESTION_POLL_MS = 2_000;

interface QuestionState {
  scope: string;
  snapshot: AgentQuestionsApi | null;
  error: string | null;
  submitting: boolean;
}

interface QuestionOperations {
  refresh: () => Promise<void>;
  answer: (
    request: AgentQuestionRequestApi,
    answers: Record<string, string[]>,
    reject: boolean,
  ) => Promise<void>;
}

/** Native request IDs are only meaningful inside the exact live turn that issued them. */
export function useNativeQuestions({
  threadId,
  jobId,
  attemptId,
  active,
}: {
  threadId: string;
  jobId: string;
  attemptId?: number | null;
  active: boolean;
}) {
  const scope = JSON.stringify([threadId, jobId, attemptId ?? null, active]);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const [state, setState] = useState<QuestionState>({
    scope,
    snapshot: null,
    error: null,
    submitting: false,
  });
  const operations = useRef<QuestionOperations | null>(null);

  useEffect(() => {
    let disposed = false;
    let reading = false;
    let readAgain = false;
    let submitting = false;
    let readVersion = 0;
    let answerError: string | null = null;
    const controller = new AbortController();
    let snapshot: AgentQuestionsApi | null = null;
    const current = () => !disposed && currentScope.current === scope;
    const update = (value: Partial<QuestionState>) => {
      if (current()) setState((previous) => ({ ...previous, scope, ...value }));
    };
    setState({ scope, snapshot: null, error: null, submitting: false });
    if (!active || !threadId || !jobId || typeof attemptId !== 'number') {
      operations.current = null;
      return;
    }

    async function refresh() {
      if (!current() || submitting || document.visibilityState === 'hidden') return;
      if (reading) {
        readAgain = true;
        return;
      }
      reading = true;
      const version = readVersion;
      try {
        const result = await listThreadQuestions(threadId, controller.signal);
        if (!current() || version !== readVersion) return;
        // A refresh can race a turn transition. Only the active generation's
        // own snapshot can put an answer button on this page.
        const next =
          result.job_id === jobId && result.attempt_id === (attemptId ?? null) ? result : null;
        if (next?.questions[0]?.request_id !== snapshot?.questions[0]?.request_id)
          answerError = null;
        snapshot = next;
        update({ snapshot, error: answerError });
      } catch (cause) {
        if (current() && version === readVersion) update({ error: getErrorMessage(cause) });
      } finally {
        reading = false;
        if (readAgain && current() && !submitting) {
          readAgain = false;
          void refresh();
        }
      }
    }

    async function answer(
      request: AgentQuestionRequestApi,
      answers: Record<string, string[]>,
      reject: boolean,
    ) {
      if (!current() || submitting || !snapshot?.job_id || snapshot.attempt_id === null) return;
      if (!snapshot.questions.some((question) => question.request_id === request.request_id))
        return;
      const owner = snapshot;
      submitting = true;
      answerError = null;
      // Invalidate a poll already in flight: its pre-answer snapshot must not
      // revive this request after the host accepts the answer.
      readVersion += 1;
      update({ submitting: true, error: null });
      try {
        const result = await answerThreadQuestion(threadId, {
          request_id: request.request_id,
          job_id: owner.job_id!,
          attempt_id: owner.attempt_id!,
          answers,
          ...(reject ? { reject: true } : {}),
        });
        if (!current()) return;
        if (!result.accepted) {
          answerError =
            'This question is no longer available. Refresh to check for a new question.';
          update({ error: answerError });
          return;
        }
        snapshot = {
          ...owner,
          questions: owner.questions.filter(
            (question) => question.request_id !== request.request_id,
          ),
        };
        update({ snapshot });
      } catch (cause) {
        answerError = getErrorMessage(cause);
        if (current()) update({ error: answerError });
      } finally {
        submitting = false;
        update({ submitting: false });
      }
      // Re-read the host after an accepted answer. A request waiting behind it
      // is discovered without requiring the user to reload the conversation.
      await refresh();
    }

    operations.current = { refresh, answer };
    void refresh();
    const interval = setInterval(() => void refresh(), QUESTION_POLL_MS);
    const onVisible = () => {
      if (document.visibilityState !== 'hidden') void refresh();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      disposed = true;
      controller.abort();
      clearInterval(interval);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [scope, active, threadId, jobId, attemptId]);

  const visible = active && state.scope === scope;
  return {
    request: visible ? (state.snapshot?.questions[0] ?? null) : null,
    error: visible ? state.error : null,
    submitting: visible && state.submitting,
    refresh: () => operations.current?.refresh(),
    answer: (request: AgentQuestionRequestApi, answers: Record<string, string[]>, reject = false) =>
      operations.current?.answer(request, answers, reject),
    scope,
  };
}
