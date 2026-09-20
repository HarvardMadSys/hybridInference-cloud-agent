'use client';
import type { TextareaHTMLAttributes } from 'react';
import { isComposerSubmit } from './composerKeys';
import type { AgentJob } from './types';

export function composerModels(models: string[], currentModel: string) {
  return models.length && !models.includes(currentModel) ? [currentModel, ...models] : models;
}
export function composerOverrides(job: AgentJob, model: string, effort: string) {
  return {
    ...(model && model !== job.model ? { model } : {}),
    ...(effort && effort !== job.reasoningEffort ? { reasoning_effort: effort } : {}),
  };
}
export function ComposerTextarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea
      {...props}
      onKeyDown={(event) => {
        if (!isComposerSubmit(event)) return;
        event.preventDefault();
        event.currentTarget.form?.requestSubmit();
      }}
    />
  );
}
export function ComposerSendButton({
  label,
  title,
  busy,
  disabled,
}: {
  label: string;
  title: string;
  busy: boolean;
  disabled: boolean;
}) {
  return (
    <button
      type="submit"
      disabled={disabled}
      className="ml-auto inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-crimson text-white shadow-sm transition-colors hover:bg-crimson-dark disabled:cursor-not-allowed disabled:opacity-40"
      aria-label={label}
      title={title}
    >
      {busy ? (
        <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white" />
      ) : (
        <svg
          aria-hidden="true"
          className="h-4 w-4"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path strokeLinecap="round" strokeLinejoin="round" d="M12 19V5m0 0-6 6m6-6 6 6" />
        </svg>
      )}
    </button>
  );
}
