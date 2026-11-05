'use client';

import { useModalDialog } from '@/components/ui/useModalDialog';
import { Markdown } from '@/components/ui/Markdown';

import { useId, useRef, useState, type FormEvent } from 'react';
import { createPortal } from 'react-dom';
import type { AgentQuestionRequestApi } from '@/lib/api/questions';
import { useNativeQuestions } from './useNativeQuestions';

interface QuestionAnswer {
  selected: string[];
  custom: boolean;
  text: string;
}

function QuestionDialog({
  request,
  submitting,
  error,
  onAnswer,
  onDismiss,
  onRefresh,
  open,
}: {
  request: AgentQuestionRequestApi;
  submitting: boolean;
  error: string | null;
  onAnswer: (answers: Record<string, string[]>, reject?: boolean) => void;
  onDismiss: () => void;
  onRefresh: () => void;
  open: boolean;
}) {
  const titleId = useId();
  const panel = useRef<HTMLDivElement>(null);
  const [answers, setAnswers] = useState<QuestionAnswer[]>(() =>
    request.questions.map((question) => ({
      selected: [],
      custom: !question.options.length,
      text: '',
    })),
  );

  useModalDialog(panel, {
    open,
    busy: submitting,
    onClose: onDismiss,
    initialFocus: 'input, textarea',
  });

  function update(index: number, value: Partial<QuestionAnswer>) {
    setAnswers((previous) =>
      previous.map((answer, i) => (i === index ? { ...answer, ...value } : answer)),
    );
  }

  const values = answers.map((answer, index) => [
    ...answer.selected,
    ...(request.questions[index].allow_custom && answer.custom && answer.text.trim()
      ? [answer.text.trim()]
      : []),
  ]);
  const complete =
    values.length > 0 &&
    values.every((value) => value.length > 0 && value.length <= 20) &&
    answers.every((answer) => !answer.custom || Boolean(answer.text.trim()));

  function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting || !complete) return;
    onAnswer(
      Object.fromEntries(request.questions.map((question, index) => [question.id, values[index]])),
      request.questions.some(
        (question, index) => question.kind === 'plan_approval' && answers[index].custom,
      ),
    );
  }

  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-[60] flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-gray-900/25 backdrop-blur-[2px] dark:bg-black/50" />
      <div
        ref={panel}
        role="dialog"
        lang="en"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="relative flex max-h-full w-full max-w-xl flex-col overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-card dark:border-white/10 dark:bg-gray-900"
      >
        <div className="shrink-0 border-b border-gray-200 px-6 py-4 dark:border-white/10">
          <h2 id={titleId} className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            The agent has a question
          </h2>
        </div>
        <form className="flex min-h-0 flex-col" onSubmit={submit}>
          <div className="min-h-0 overflow-y-auto px-6 py-5">
            {request.questions.map((question, index) => {
              const answer = answers[index];
              const name = `${titleId}-question-${index}`;
              return (
                <fieldset
                  key={question.id}
                  disabled={submitting}
                  className={index ? 'mt-6' : undefined}
                >
                  <legend className="text-sm font-semibold text-gray-900 dark:text-gray-100">
                    {question.kind === 'plan_approval' ? (
                      question.header || 'Plan approval'
                    ) : (
                      <>
                        {question.header ? (
                          <span className="mb-1 block">{question.header}</span>
                        ) : null}
                        <span className="block whitespace-pre-wrap break-words">
                          {question.question}
                        </span>
                      </>
                    )}
                  </legend>
                  {question.kind === 'plan_approval' ? (
                    <div className="mt-3 max-h-[50vh] overflow-y-auto rounded-lg border border-gray-200 px-4 py-3 text-gray-900 dark:border-white/10 dark:text-gray-100">
                      <Markdown text={question.question} />
                    </div>
                  ) : null}
                  {question.multiple && question.options.length ? (
                    <p className="mt-1 text-xs text-gray-500 dark:text-gray-400">
                      Select all that apply.
                    </p>
                  ) : null}
                  <div className="mt-3 space-y-2">
                    {question.options.map((option, optionIndex) => {
                      const checked = answer.selected.includes(option.label);
                      const descriptionId = `${name}-description-${optionIndex}`;
                      return (
                        <label
                          key={optionIndex}
                          className="flex cursor-pointer items-start gap-3 rounded-lg border border-gray-200 px-3 py-2.5 text-sm text-gray-900 focus-within:ring-2 focus-within:ring-crimson/25 dark:border-white/10 dark:text-gray-100"
                        >
                          <input
                            type={question.multiple ? 'checkbox' : 'radio'}
                            name={name}
                            checked={checked}
                            disabled={
                              question.multiple &&
                              !checked &&
                              answer.selected.length + Number(answer.custom) >= 20
                            }
                            aria-label={option.label}
                            aria-describedby={option.description ? descriptionId : undefined}
                            className="mt-1 accent-crimson"
                            onChange={() =>
                              update(index, {
                                selected: question.multiple
                                  ? checked
                                    ? answer.selected.filter((value) => value !== option.label)
                                    : [...answer.selected, option.label]
                                  : [option.label],
                                custom: question.multiple && answer.custom,
                              })
                            }
                          />
                          <span>
                            <span className="block font-medium">{option.label}</span>
                            {option.description ? (
                              <span
                                id={descriptionId}
                                className="mt-0.5 block text-xs text-gray-500 dark:text-gray-400"
                              >
                                {option.description}
                              </span>
                            ) : null}
                          </span>
                        </label>
                      );
                    })}
                    {question.allow_custom && question.options.length ? (
                      <label className="flex cursor-pointer items-center gap-3 rounded-lg border border-gray-200 px-3 py-2.5 text-sm text-gray-900 dark:border-white/10 dark:text-gray-100">
                        <input
                          type={question.multiple ? 'checkbox' : 'radio'}
                          name={name}
                          checked={answer.custom}
                          disabled={
                            question.multiple && !answer.custom && answer.selected.length >= 20
                          }
                          className="accent-crimson"
                          onChange={() =>
                            update(index, {
                              custom: !answer.custom,
                              selected: question.multiple ? answer.selected : [],
                            })
                          }
                        />
                        {question.kind === 'plan_approval' ? 'Request changes' : 'Other'}
                      </label>
                    ) : null}
                    {question.allow_custom && answer.custom ? (
                      <label className="block text-xs font-medium text-gray-700 dark:text-gray-300">
                        {question.kind === 'plan_approval' ? 'Your feedback' : 'Your answer'}
                        <textarea
                          value={answer.text}
                          onChange={(event) => update(index, { text: event.target.value })}
                          rows={3}
                          maxLength={10_000}
                          className="mt-1 w-full rounded-lg border border-gray-200 bg-transparent px-3 py-2 text-sm text-gray-900 focus:border-crimson/40 focus:outline-none focus:ring-2 focus:ring-crimson/20 dark:border-white/10 dark:text-gray-100"
                        />
                      </label>
                    ) : null}
                  </div>
                </fieldset>
              );
            })}
            {error ? (
              <div role="alert" className="mt-4 text-sm text-red-600 dark:text-red-400">
                {error}
                <button
                  type="button"
                  onClick={onRefresh}
                  disabled={submitting}
                  className="ml-2 underline"
                >
                  Refresh
                </button>
              </div>
            ) : null}
          </div>
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-gray-200 px-6 py-4 dark:border-white/10">
            <button
              type="button"
              disabled={submitting}
              onClick={() => onAnswer({}, true)}
              className="text-sm text-gray-500 hover:underline disabled:opacity-50 dark:text-gray-400"
            >
              Skip question
            </button>
            <div className="flex gap-3">
              <button
                type="button"
                disabled={submitting}
                onClick={onDismiss}
                className="text-sm text-gray-600 disabled:opacity-50 dark:text-gray-400"
              >
                Answer later
              </button>
              <button
                type="submit"
                disabled={submitting || !complete}
                className="rounded-lg bg-gray-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-gray-100 dark:text-gray-900"
              >
                {submitting ? 'Sending…' : 'Send answer'}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>,
    document.body,
  );
}

export function NativeQuestionPrompt(props: {
  threadId: string;
  jobId: string;
  attemptId?: number | null;
  active: boolean;
}) {
  const { request, error, submitting, refresh, answer, scope } = useNativeQuestions(props);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const key = request ? `${scope}:${request.request_id}` : null;
  if (!request) {
    return error ? (
      <p role="status" className="mb-3 text-xs text-gray-500 dark:text-gray-400">
        Could not check for questions.
        <button type="button" onClick={() => void refresh()} className="ml-2 underline">
          Retry
        </button>
      </p>
    ) : null;
  }
  return (
    <>
      {dismissed === key ? (
        <p role="status" className="mb-3 text-sm text-gray-600 dark:text-gray-400">
          The agent is waiting for your answer.
          <button
            type="button"
            onClick={() => setDismissed(null)}
            className="ml-2 font-medium underline"
          >
            Review question
          </button>
        </p>
      ) : null}
      <QuestionDialog
        key={key}
        request={request}
        submitting={submitting}
        error={error}
        onAnswer={(answers, reject) => void answer(request, answers, reject)}
        onDismiss={() => setDismissed(key)}
        onRefresh={() => void refresh()}
        open={dismissed !== key}
      />
    </>
  );
}
