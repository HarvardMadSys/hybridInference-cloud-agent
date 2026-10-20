import { useEffect, useRef, useState } from 'react';

import type { AdminRunFilters } from '@/lib/api/adminRuns';
import { getErrorMessage } from '@/lib/utils/errors';

import { accessDenied } from './shared';

/** What every cursor-paged admin listing returns besides its rows. */
export interface CursorPage {
  since: string;
  until: string;
  next_cursor: string | null;
}

/**
 * One cursor-paged admin listing: its first page for `filters`, then each
 * "Load more" appended by `append`.
 *
 * A filter change or a retry aborts the first read and any load-more still in
 * flight, then starts over. A refusal calls `onDenied` instead of showing an
 * error. `fetchPage` must keep its identity across renders, as a module-level
 * function does.
 */
export function useCursorPages<Page extends CursorPage>(
  fetchPage: (filters: AdminRunFilters, signal: AbortSignal) => Promise<Page>,
  filters: AdminRunFilters,
  append: (previous: Page, next: Page) => Page,
  { onLoading, onDenied }: { onLoading: (loading: boolean) => void; onDenied: () => void },
) {
  const [page, setPage] = useState<Page | null>(null);
  const [loading, setLoading] = useState(true);
  const [moreLoading, setMoreLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const moreRequest = useRef<AbortController | null>(null);

  useEffect(() => onLoading(loading), [loading, onLoading]);

  useEffect(() => {
    const controller = new AbortController();
    moreRequest.current?.abort();
    setMoreLoading(false);
    setPage(null);
    setLoading(true);
    setError(null);
    fetchPage(filters, controller.signal)
      .then((next) => {
        if (!controller.signal.aborted) setPage(next);
      })
      .catch((e: unknown) => {
        if (!controller.signal.aborted) {
          if (accessDenied(e)) onDenied();
          else setError(getErrorMessage(e));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => {
      controller.abort();
      moreRequest.current?.abort();
    };
  }, [fetchPage, filters, revision, onDenied]);

  async function loadMore() {
    if (!page?.next_cursor || moreLoading) return;
    moreRequest.current?.abort();
    const controller = new AbortController();
    moreRequest.current = controller;
    setMoreLoading(true);
    setError(null);
    try {
      const next = await fetchPage(
        { ...filters, since: page.since, until: page.until, cursor: page.next_cursor },
        controller.signal,
      );
      if (!controller.signal.aborted)
        setPage((previous) => (previous ? append(previous, next) : next));
    } catch (e) {
      if (!controller.signal.aborted) {
        if (accessDenied(e)) onDenied();
        else setError(getErrorMessage(e));
      }
    } finally {
      if (!controller.signal.aborted) setMoreLoading(false);
    }
  }

  return {
    page,
    loading,
    moreLoading,
    error,
    retry: () => setRevision((value) => value + 1),
    loadMore,
  };
}
