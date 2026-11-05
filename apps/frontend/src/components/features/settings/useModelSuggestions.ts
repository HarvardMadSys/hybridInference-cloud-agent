'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { discoverModels, type ModelListRequest } from '@/lib/api/modelConnections';
import { APIError } from '@/lib/utils/errors';

/** Why no list was loaded, in terms the owner can act on; never the provider's text. */
export type SuggestionProblem =
  | 'unsupported'
  | 'auth'
  | 'unreachable'
  | 'rateLimited'
  | 'limited'
  | 'keyRequired'
  | 'failed';

const PROBLEMS: Readonly<Record<string, SuggestionProblem>> = {
  model_connection_endpoint_missing: 'unsupported',
  model_connection_invalid_response: 'unsupported',
  model_connection_too_large: 'unsupported',
  model_connection_auth: 'auth',
  model_connection_blocked_address: 'unreachable',
  model_connection_unreachable: 'unreachable',
  model_connection_tls: 'unreachable',
  model_connection_redirected: 'unreachable',
  model_connection_upstream_error: 'unreachable',
  model_connection_rate_limited: 'rateLimited',
  model_connection_check_limit: 'limited',
  model_connection_key_required: 'keyRequired',
};

export function suggestionProblem(cause: unknown): SuggestionProblem {
  if (cause instanceof APIError && Object.hasOwn(PROBLEMS, cause.code)) return PROBLEMS[cause.code];
  return 'failed';
}

export type Suggestions =
  | { phase: 'loading' }
  | { phase: 'loaded'; models: string[] }
  | { phase: 'failed'; problem: SuggestionProblem };

/** One answer, and the exact request it answers. */
interface Answer {
  source: string;
  value: Suggestions;
}

/**
 * The provider's model IDs for the form's current Base URL and key, loaded
 * only when the owner asks (`load`). An answer is shown only while the form
 * still has the exact URL and key it was asked with: changing either, a
 * newer request, `stop` (a save starting) or unmounting aborts what is in
 * flight, and a late answer is dropped. Loading never touches the form's
 * models or draft, and never blocks typing.
 */
export function useModelSuggestions(request: ModelListRequest | null) {
  // Compared, never sent anywhere or logged: the request already holds the key.
  const source = request ? JSON.stringify(request) : '';
  const [answer, setAnswer] = useState<Answer | null>(null);
  const inFlight = useRef<{ seq: number; abort: AbortController | null }>({ seq: 0, abort: null });

  const abort = useCallback(() => {
    inFlight.current.seq += 1;
    inFlight.current.abort?.abort();
    inFlight.current.abort = null;
  }, []);

  // A new URL or key, or leaving the form, ends whatever was asked before.
  useEffect(() => abort, [source, abort]);

  async function load() {
    if (!request) return;
    abort();
    const seq = inFlight.current.seq;
    const controller = new AbortController();
    inFlight.current.abort = controller;
    setAnswer({ source, value: { phase: 'loading' } });
    let value: Suggestions;
    try {
      value = { phase: 'loaded', models: await discoverModels(request, controller.signal) };
    } catch (cause) {
      value = { phase: 'failed', problem: suggestionProblem(cause) };
    }
    if (seq !== inFlight.current.seq) return;
    inFlight.current.abort = null;
    setAnswer({ source, value });
  }

  /** Abort a request in flight; a list already shown stays. */
  function stop() {
    abort();
    setAnswer((current) => (current?.value.phase === 'loading' ? null : current));
  }

  const current = answer && answer.source === source ? answer.value : null;
  return { suggestions: current, load, stop };
}
