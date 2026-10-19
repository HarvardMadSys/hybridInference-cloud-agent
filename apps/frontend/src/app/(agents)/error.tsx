'use client';

import { useT } from '@/components/providers';

/**
 * What a page in the agent shell shows when it throws while rendering.
 *
 * Without a boundary, one value the UI could not draw — a stored message, a
 * workspace entry — unmounted the whole app, and reopening that conversation
 * did it again. This stands in for the page alone: the header and sidebar
 * around it stay usable, and Next clears it on the next navigation. The
 * error's own text is for the console, where React has already logged it.
 */
export default function AgentsError({
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  const t = useT();
  return (
    <div className="flex h-full flex-col items-center justify-center gap-3 px-4 text-center">
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
