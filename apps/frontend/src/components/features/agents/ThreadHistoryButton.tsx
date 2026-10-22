'use client';

import type { ThreadHistoryControl } from './useThreadHistory';

export function ThreadHistoryButton({ history }: { history?: ThreadHistoryControl }) {
  if (!history?.hasOlder) return null;
  return (
    <div className="px-4 py-2 text-center text-xs text-muted">
      <button
        type="button"
        className="ui-focus-ring rounded px-2 py-1 underline"
        disabled={history.loading}
        onClick={history.loadOlder}
      >
        {history.loading ? 'Loading history…' : 'Load older conversation history'}
      </button>
      {history.error && <p role="alert">{history.error}</p>}
    </div>
  );
}
