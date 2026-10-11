'use client';
import type { Ref, TextareaHTMLAttributes } from 'react';
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
export function ComposerTextarea({
  onKeyDown,
  textareaRef,
  className,
  ...props
}: TextareaHTMLAttributes<HTMLTextAreaElement> & {
  /**
   * The box itself.
   *
   * A named prop rather than a forwarded `ref`: a slash command *writes into*
   * this box rather than opening anything, and clicking the menu row moves
   * focus to the row — so the composer that chose the command needs the
   * element back to put the caret where the next character goes.
   */
  textareaRef?: Ref<HTMLTextAreaElement>;
}) {
  return (
    <textarea
      {...props}
      ref={textareaRef}
      className={`${className ?? ''} [@media(pointer:coarse)]:text-base`}
      onKeyDown={(event) => {
        // A caller's handler runs first and has the last word: the command menu
        // consumes the keys it owns — including the Enter that would otherwise
        // submit the form — and says so by preventing the default.
        onKeyDown?.(event);
        if (event.defaultPrevented) return;
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
      className="ui-focus-ring ml-auto inline-flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-crimson text-white shadow-sm transition-colors hover:bg-crimson-dark disabled:cursor-not-allowed disabled:opacity-40 [@media(pointer:coarse)]:h-11 [@media(pointer:coarse)]:w-11"
      aria-label={label}
      title={title}
    >
      {busy ? (
        <span
          aria-hidden="true"
          className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-white/40 border-t-white motion-reduce:animate-none"
        />
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

/** Visible send modes; the parent offers steering only when this draft can use it. */
export function ComposerMessageActions({
  label,
  title,
  busy,
  disabled,
  steerLabel,
  steerTitle,
  onSteer,
}: {
  label: string;
  title: string;
  busy: boolean;
  disabled: boolean;
  steerLabel?: string;
  steerTitle?: string;
  onSteer?: () => void;
}) {
  return (
    <div className="ml-auto flex shrink-0 items-center gap-2">
      {onSteer && steerLabel ? (
        <button
          type="button"
          onClick={onSteer}
          disabled={disabled || busy}
          aria-keyshortcuts="Meta+Enter Control+Enter"
          title={steerTitle}
          className="ui-focus-ring inline-flex min-h-10 items-center rounded-xl border border-gray-200 px-3 text-[13px] font-medium text-gray-700 transition-colors hover:bg-gray-100 disabled:cursor-not-allowed disabled:opacity-40 dark:border-white/15 dark:text-gray-200 dark:hover:bg-white/10 [@media(pointer:coarse)]:min-h-11"
        >
          {steerLabel}
        </button>
      ) : null}
      <button
        type="submit"
        disabled={disabled || busy}
        title={title}
        className="ui-focus-ring inline-flex min-h-10 items-center gap-2 rounded-xl bg-crimson px-4 text-[13px] font-semibold text-white shadow-sm transition-colors hover:bg-crimson-dark disabled:cursor-not-allowed disabled:opacity-40 [@media(pointer:coarse)]:min-h-11"
      >
        {busy ? (
          <span
            aria-hidden="true"
            className="h-3 w-3 animate-spin rounded-full border-2 border-white/40 border-t-white motion-reduce:animate-none"
          />
        ) : null}
        {label}
      </button>
    </div>
  );
}
