'use client';

// The People view's filters live in the address bar, as HybridInference's
// users table keeps them: a reload, a shared link or a link from elsewhere in
// Admin (a team's members, one person) opens the same rows.

import { useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useRef, useState } from 'react';
import type { AccountFilters, WaitlistStatus } from '@/lib/api/accounts';

export interface UserFilters {
  status: WaitlistStatus | '';
  state: NonNullable<AccountFilters['accountStatus']>;
  sort: 'created' | 'last_login';
  q: string;
  team: string;
}

const STATUSES: readonly WaitlistStatus[] = [
  'pending',
  'approved',
  'rejected',
  'revoked',
  'not_applied',
];
const STATES = ['active', 'suspended', 'deleted'] as const;

function oneOf<T extends string>(value: string | null, allowed: readonly T[]): T | '' {
  return allowed.find((option) => option === value) ?? '';
}

/** What a query asks for; a value the page does not offer reads as no filter. */
export function filtersFromQuery(query: string): UserFilters {
  const params = new URLSearchParams(query);
  return {
    status: oneOf(params.get('status'), STATUSES),
    state: oneOf(params.get('state'), STATES),
    sort: params.get('sort') === 'last_login' ? 'last_login' : 'created',
    q: (params.get('q') ?? '').trim().slice(0, 320),
    team: (params.get('team') ?? '').trim().slice(0, 128),
  };
}

/** The query for these filters, leaving out every default. */
export function queryFromFilters(filters: UserFilters): string {
  const params = new URLSearchParams();
  if (filters.status) params.set('status', filters.status);
  if (filters.state) params.set('state', filters.state);
  if (filters.sort !== 'created') params.set('sort', filters.sort);
  if (filters.q) params.set('q', filters.q);
  if (filters.team) params.set('team', filters.team);
  return params.toString();
}

export type FilterChange = UserFilters | ((current: UserFilters) => UserFilters);

/**
 * The filters, read from the address bar and written back to it in place. A
 * query this page did not write (back and forward, a link to it) replaces
 * them; the one it wrote is already on screen.
 */
export function useUserFilters(): [UserFilters, (change: FilterChange) => void] {
  const query = useSearchParams()?.toString() ?? '';
  const [filters, setFilters] = useState(() => filtersFromQuery(query));
  const written = useRef(query);
  // What a change made from a timer starts from: the filters now, not the ones
  // on screen when the timer was set.
  const latest = useRef(filters);
  useEffect(() => {
    latest.current = filters;
  }, [filters]);
  useEffect(() => {
    if (query === written.current) return;
    written.current = query;
    setFilters(filtersFromQuery(query));
  }, [query]);
  const update = useCallback((change: FilterChange) => {
    const next = typeof change === 'function' ? change(latest.current) : change;
    if (next === latest.current) return;
    latest.current = next;
    const nextQuery = queryFromFilters(next);
    written.current = nextQuery;
    window.history.replaceState(null, '', nextQuery ? `?${nextQuery}` : window.location.pathname);
    setFilters(next);
  }, []);
  return [filters, update];
}
