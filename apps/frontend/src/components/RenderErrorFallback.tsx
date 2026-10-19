'use client';

import { useT } from '@/components/providers/LocaleProvider';

/**
 * What an error boundary shows in place of the part of the app that threw while
 * rendering, with a Retry that renders that part again.
 *
 * The three boundaries differ only in how much they replace: `(agents)/error`
 * one page inside the agent shell, `error` a page outside it or the shell's own
 * layout, and `global-error` the root layout, document and all. `h-full` fills
 * the shell's page area; `flex-1` fills the body's column where there is no
 * shell. The error's own text is for the console, where React has already
 * logged it.
 */
export function RenderErrorFallback({ reset }: { reset: () => void }) {
  const t = useT();
  return (
    <div className="flex h-full flex-1 flex-col items-center justify-center gap-3 px-4 text-center">
      <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
        {t('shell.renderError.title')}
      </h1>
      <p className="text-sm text-gray-500 dark:text-gray-400">{t('shell.renderError.body')}</p>
      <button type="button" onClick={reset} className="ui-button ui-button-secondary min-h-11">
        {t('common.retry')}
      </button>
    </div>
  );
}
