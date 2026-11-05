'use client';

// DNA bands for the trend's visible requests, read in windows of at most 60
// and kept in a small cache per conversation (as dsh-context keeps its band
// store, `trendChart.tsx`, LRU 512). Only while DNA is shown; a stale reply
// for another conversation or window is dropped.

import { useEffect, useMemo, useRef, useState } from 'react';
import { getThreadContextDna, type DnaBandsApi } from '@/lib/api/agents';
import type { DnaItems } from './trendModel';

/** Visible requests DNA draws at once (two reads). */
export const DNA_MAX_COLUMNS = 120;
const CHUNK = 60;
const CACHE_MAX = 512;

/** The key of the band that stands for a request's earliest, folded items. */
export const FOLDED_KEY = 'folded';

/** A request's bands, its folded earliest items first as one band, so the column keeps its size. */
export function withFolded(request: DnaBandsApi['requests'][number]): DnaItems {
  if (request.folded_tokens <= 0) return request.items;
  return [[FOLDED_KEY, FOLDED_KEY, request.folded_tokens], ...request.items];
}

/** Ranges of at most `CHUNK` request indexes covering every missing one. */
export function dnaRanges(missing: number[]): [number, number][] {
  const sorted = [...missing].sort((a, b) => a - b);
  const ranges: [number, number][] = [];
  for (const index of sorted) {
    const last = ranges.at(-1);
    if (last && index - last[0] < CHUNK) last[1] = index;
    else ranges.push([index, index]);
  }
  return ranges;
}

export function useDnaBands(
  jobId: string,
  conversationKey: string,
  indexes: number[],
): { bands: ReadonlyMap<number, DnaItems>; loading: boolean } {
  const cache = useRef<{ key: string; bands: Map<number, DnaItems> }>({
    key: conversationKey,
    bands: new Map(),
  });
  if (cache.current.key !== conversationKey)
    cache.current = { key: conversationKey, bands: new Map() };
  const [version, setVersion] = useState(0);
  const [loading, setLoading] = useState(false);
  const wanted = indexes.join(',');
  useEffect(() => {
    if (!wanted) return undefined;
    const store = cache.current;
    const missing = wanted
      .split(',')
      .map(Number)
      .filter((index) => !store.bands.has(index));
    if (missing.length === 0) return undefined;
    const controller = new AbortController();
    setLoading(true);
    Promise.all(
      dnaRanges(missing).map(([from, to]) =>
        getThreadContextDna(jobId, from, to, controller.signal),
      ),
    )
      .then((replies) => {
        if (controller.signal.aborted || cache.current !== store) return;
        for (const reply of replies) {
          for (const request of reply?.requests ?? [])
            store.bands.set(request.index, withFolded(request));
        }
        while (store.bands.size > CACHE_MAX)
          store.bands.delete(store.bands.keys().next().value as number);
        setVersion((value) => value + 1);
      })
      .catch(() => undefined)
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [jobId, wanted, conversationKey]);
  // `version` stands for the cache's contents, which a ref holds.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const bands = useMemo(() => new Map(cache.current.bands), [version, conversationKey]);
  return { bands, loading };
}
