'use client';

import { useEffect, useRef, useState } from 'react';
import {
  answerThreadQuestion,
  type AgentQuestionRequestApi,
  type AgentQuestionsApi,
} from '@/lib/api/questions';
import { getErrorMessage } from '@/lib/utils/errors';
import type { QuestionReadScope } from '@/lib/api/questionReadScope';
import { useQuestionReads } from './AgentReadProvider';

/**
 * How often the conversation on screen reads its questions. Nothing pushes a
 * new question yet, and the owner is looking at this one.
 */
export const QUESTION_POLL_MS = 2_000;
/** How often a conversation that is not on screen does: a sidebar row, a notification. */
export const QUESTION_BACKGROUND_POLL_MS = 12_000;

interface QuestionView {
  snapshot: AgentQuestionsApi | null;
  error: string | null;
  submitting: boolean;
}

interface QuestionState extends QuestionView {
  reads: QuestionReadScope;
  scope: string;
}

interface QuestionOperations {
  refresh: () => Promise<void>;
  answer: (
    request: AgentQuestionRequestApi,
    answers: Record<string, string[]>,
    reject: boolean,
  ) => Promise<void>;
}

/** Whether a read changes nothing the page shows: the request on offer, the error, the send. */
function sameView(shown: QuestionView, next: QuestionView): boolean {
  return (
    shown.snapshot?.questions[0]?.request_id === next.snapshot?.questions[0]?.request_id &&
    shown.error === next.error &&
    shown.submitting === next.submitting
  );
}

/**
 * Native request IDs are only meaningful inside the exact live turn that issued them.
 *
 * Reads on the tab's one clock for the conversation, every `intervalMs` at
 * most: the default is for the conversation on screen.
 */
export function useNativeQuestions({
  threadId,
  jobId,
  attemptId,
  active,
  intervalMs = QUESTION_POLL_MS,
}: {
  threadId: string;
  jobId: string;
  attemptId?: number | null;
  active: boolean;
  intervalMs?: number;
}) {
  const reads = useQuestionReads();
  const scope = JSON.stringify([threadId, jobId, attemptId ?? null, active]);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const currentReads = useRef(reads);
  currentReads.current = reads;
  const [state, setState] = useState<QuestionState>({
    reads,
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
    // What this scope last put on the page. A request ID names one immutable
    // native request, so a poll that finds the same one renders nothing.
    let shown: QuestionView = { snapshot: null, error: null, submitting: false };
    const current = () =>
      !disposed && currentScope.current === scope && currentReads.current === reads;
    const update = (value: Partial<QuestionView>) => {
      if (!current()) return;
      const next = { ...shown, ...value };
      if (sameView(shown, next)) return;
      shown = next;
      setState({ reads, scope, ...next });
    };
    setState({ reads, scope, ...shown });
    if (!active || !threadId || !jobId || typeof attemptId !== 'number') {
      operations.current = null;
      return;
    }

    async function refresh() {
      if (!current() || submitting) return;
      if (reading) {
        readAgain = true;
        return;
      }
      reading = true;
      const version = readVersion;
      try {
        const result = await reads.listThreadQuestions(threadId, controller.signal);
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
        // Shared reads are cancelled when the shell invalidates its cache.
        // Keep the known question until the next poll confirms its state.
        if (
          typeof cause === 'object' &&
          cause !== null &&
          'name' in cause &&
          cause.name === 'AbortError'
        )
          return;
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
        reads.invalidate(threadId);
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
    // The clock reads now, then on its ticks, and pauses while the tab is hidden.
    const leave = reads.watch(threadId, { intervalMs }, () => void refresh());
    return () => {
      disposed = true;
      controller.abort();
      leave();
    };
  }, [scope, active, threadId, jobId, attemptId, reads, intervalMs]);

  const visible = active && state.scope === scope && state.reads === reads;
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
