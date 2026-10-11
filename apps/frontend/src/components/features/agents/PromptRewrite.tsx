'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { rewritePrompt, type PromptClarificationAnswer } from '@/lib/api/promptRewrite';

interface PromptRewriteProps {
  value: string;
  model: string;
  disabled?: boolean;
  compact?: boolean;
  onApply: (prompt: string) => void;
  onActiveChange?: (active: boolean) => void;
}

export function PromptRewrite({
  value,
  model,
  disabled = false,
  compact = false,
  onApply,
  onActiveChange,
}: PromptRewriteProps) {
  const t = useT();
  const id = useId();
  const [clarify, setClarify] = useState(false);
  const [pending, setPending] = useState(false);
  const [questions, setQuestions] = useState<string[]>([]);
  const [answers, setAnswers] = useState<string[]>([]);
  const [error, setError] = useState<'rewrite.error' | 'rewrite.invalidResponse' | null>(null);
  const [appliedPrompt, setAppliedPrompt] = useState<string | null>(null);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const latest = useRef({ value, model });
  latest.current = { value, model };
  const onActiveChangeRef = useRef(onActiveChange);
  onActiveChangeRef.current = onActiveChange;
  const reportedActivity = useRef<boolean | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const firstAnswer = useRef<HTMLTextAreaElement>(null);
  const restoreFocus = useRef(false);
  const active = pending || questions.length > 0;

  const reportActivity = useCallback((next: boolean) => {
    if (reportedActivity.current === next) return;
    reportedActivity.current = next;
    onActiveChangeRef.current?.(next);
  }, []);

  // An edit or model change starts a different draft. Even APIs that do not
  // honor abort must never replace that draft with an older response.
  useEffect(() => {
    generation.current += 1;
    controller.current?.abort();
    controller.current = null;
    setPending(false);
    setQuestions([]);
    setAnswers([]);
    setError(null);
    setAppliedPrompt((previous) => (previous === value ? previous : null));
    reportActivity(false);
  }, [value, model, reportActivity]);

  useEffect(
    () => () => {
      generation.current += 1;
      controller.current?.abort();
      reportActivity(false);
    },
    [reportActivity],
  );

  useEffect(() => {
    if (questions.length) firstAnswer.current?.focus();
  }, [questions]);

  useEffect(() => {
    if (!active && restoreFocus.current) {
      restoreFocus.current = false;
      trigger.current?.focus();
    }
  }, [active]);

  function cancel() {
    generation.current += 1;
    controller.current?.abort();
    controller.current = null;
    setPending(false);
    setQuestions([]);
    setAnswers([]);
    setError(null);
    restoreFocus.current = true;
    reportActivity(false);
  }

  async function rewrite(clarifications?: PromptClarificationAnswer[]) {
    if (disabled || controller.current || !value.trim() || !model) return;
    const requestId = ++generation.current;
    const source = { value, model };
    const abort = new AbortController();
    controller.current = abort;
    setPending(true);
    setError(null);
    setAppliedPrompt(null);
    reportActivity(true);
    let waitingForAnswers = questions.length > 0;
    const current = () =>
      requestId === generation.current &&
      latest.current.value === source.value &&
      latest.current.model === source.model;
    try {
      const result = await rewritePrompt(
        {
          prompt: source.value,
          model: source.model,
          clarify: clarifications ? false : clarify,
          ...(clarifications ? { answers: clarifications } : {}),
        },
        abort.signal,
      );
      if (!current()) return;
      if (result.kind === 'questions') {
        if (
          clarifications ||
          !clarify ||
          result.questions.length < 2 ||
          result.questions.length > 4 ||
          result.questions.some((question) => !question.trim())
        ) {
          setError('rewrite.invalidResponse');
          return;
        }
        waitingForAnswers = true;
        setQuestions(result.questions);
        setAnswers(result.questions.map(() => ''));
      } else if (result.prompt.trim()) {
        waitingForAnswers = false;
        setQuestions([]);
        setAnswers([]);
        setAppliedPrompt(result.prompt);
        onApply(result.prompt);
      } else {
        setError('rewrite.invalidResponse');
      }
    } catch {
      if (current()) setError('rewrite.error');
    } finally {
      if (current()) {
        controller.current = null;
        setPending(false);
        reportActivity(waitingForAnswers);
      }
    }
  }

  const complete = questions.length > 0 && answers.every((answer) => answer.trim());

  return (
    <div className={compact ? 'space-y-2 px-3 pb-1' : 'space-y-3 px-4 pb-3'}>
      <div
        className={`flex flex-wrap items-center ${compact ? 'gap-x-3 gap-y-1' : 'gap-x-4 gap-y-2'}`}
      >
        <button
          ref={trigger}
          type="button"
          disabled={disabled || active || !value.trim() || !model}
          onClick={() => void rewrite()}
          className={`ui-focus-ring inline-flex ${compact ? 'min-h-7' : 'min-h-9'} items-center gap-1.5 rounded-lg px-2 py-1 text-xs font-medium text-gray-600 transition-colors hover:bg-gray-100 hover:text-gray-900 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100 [@media(pointer:coarse)]:min-h-11`}
        >
          <svg
            viewBox="0 0 20 20"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            aria-hidden="true"
            className="h-3.5 w-3.5"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="m4 16 9-9 3-3-2-2-3 3-9 9v2h2Zm6-10 2 2M5 2v4M3 4h4M15 12v5M12.5 14.5h5"
            />
          </svg>
          {t('rewrite.button')}
        </button>
        <label
          className={`flex ${compact ? 'min-h-7' : 'min-h-9'} cursor-pointer items-center gap-2 text-xs text-gray-500 dark:text-gray-400 [@media(pointer:coarse)]:min-h-11`}
        >
          <input
            type="checkbox"
            checked={clarify}
            disabled={disabled || active}
            onChange={(event) => setClarify(event.target.checked)}
            className="accent-crimson disabled:cursor-not-allowed"
          />
          {t('rewrite.clarify')}
        </label>
        {pending ? (
          <span role="status" className="text-xs text-gray-500 dark:text-gray-400">
            {t(clarify && !questions.length ? 'rewrite.asking' : 'rewrite.generating')}
          </span>
        ) : null}
        {pending && !questions.length ? (
          <button type="button" onClick={cancel} className="ui-button ui-button-ghost text-xs">
            {t('rewrite.cancel')}
          </button>
        ) : null}
      </div>
      {questions.length ? (
        <section
          aria-labelledby={`${id}-heading`}
          aria-busy={pending}
          className="space-y-3 rounded-xl border border-gray-200 bg-gray-50/70 p-4 dark:border-white/10 dark:bg-white/[0.03]"
          onKeyDown={(event) => {
            // The enclosing composer owns submit and stop shortcuts. These
            // textareas collect answers and never submit the task itself.
            event.stopPropagation();
            if (event.key === 'Escape') {
              event.preventDefault();
              cancel();
            }
          }}
        >
          <div>
            <h3
              id={`${id}-heading`}
              className="text-sm font-medium text-gray-900 dark:text-gray-100"
            >
              {t('rewrite.questionsTitle')}
            </h3>
            <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
              {t('rewrite.questionsHint')}
            </p>
          </div>
          {questions.map((question, index) => (
            <label
              key={`${index}-${question}`}
              className="block text-xs font-medium text-gray-700 dark:text-gray-300"
            >
              {question}
              <textarea
                ref={index === 0 ? firstAnswer : undefined}
                value={answers[index]}
                disabled={disabled || pending}
                rows={2}
                maxLength={5_000}
                onChange={(event) =>
                  setAnswers((previous) =>
                    previous.map((answer, at) => (at === index ? event.target.value : answer)),
                  )
                }
                className="mt-1.5 block w-full resize-y rounded-lg border border-gray-200 bg-white px-3 py-2 text-sm font-normal text-gray-900 focus:border-crimson/40 focus:outline-none focus:ring-2 focus:ring-crimson/20 disabled:opacity-50 dark:border-white/10 dark:bg-gray-900 dark:text-gray-100 [@media(pointer:coarse)]:text-base"
              />
            </label>
          ))}
          <div className="flex items-center justify-end gap-3">
            <button type="button" onClick={cancel} className="ui-button ui-button-ghost text-xs">
              {t('rewrite.cancel')}
            </button>
            <button
              type="button"
              disabled={disabled || pending || !complete}
              onClick={() =>
                void rewrite(
                  questions.map((question, index) => ({ question, answer: answers[index].trim() })),
                )
              }
              className="rounded-lg bg-gray-900 px-3 py-2 text-xs font-medium text-white disabled:cursor-not-allowed disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900"
            >
              {t(pending ? 'rewrite.generating' : 'rewrite.generate')}
            </button>
          </div>
        </section>
      ) : null}
      {error ? (
        <p role="alert" className="text-xs text-red-600 dark:text-red-400">
          {t(error)}
        </p>
      ) : null}
      {appliedPrompt === value ? (
        <p role="status" className="text-xs text-gray-500 dark:text-gray-400">
          {t('rewrite.applied')}
        </p>
      ) : null}
    </div>
  );
}
