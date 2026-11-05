'use client';

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type Dispatch,
  type SetStateAction,
} from 'react';
import type { AgentThreadApi } from '@/lib/api/agents';
import type { AgentReadScope } from '@/lib/api/agentReadScope';
import { createThreadHistoryReader } from './threadHistory';

export interface ThreadHistoryControl {
  hasOlder: boolean;
  loading: boolean;
  error: string | null;
  loadOlder: () => void;
}

export function useThreadHistory(
  jobId: string,
  reads: AgentReadScope,
  thread: AgentThreadApi | null,
  setThread: Dispatch<SetStateAction<AgentThreadApi | null>>,
  refreshing: boolean,
) {
  const reader = useMemo(() => createThreadHistoryReader(reads), [reads]);
  const identity = useMemo(() => ({ jobId, reads }), [jobId, reads]);
  const scope = useRef(identity);
  scope.current = identity;
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pending = useRef(false);
  useEffect(() => {
    pending.current = false;
    setLoading(false);
    setError(null);
  }, [identity]);
  const loadOlder = useCallback(() => {
    if (pending.current || refreshing) return;
    const request = scope.current;
    pending.current = true;
    setLoading(true);
    setError(null);
    void reader
      .older(jobId)
      .then((page) => {
        if (scope.current === request && page) setThread(page);
      })
      .catch((cause: unknown) => {
        if (scope.current === request) {
          setError(cause instanceof Error ? cause.message : 'Could not load older history');
        }
      })
      .finally(() => {
        if (scope.current === request) {
          pending.current = false;
          setLoading(false);
        }
      });
  }, [jobId, reader, refreshing, setThread]);
  const readThread = useCallback((id: string) => reader.read(id), [reader]);
  const history: ThreadHistoryControl = {
    hasOlder: Boolean(thread?.next_cursor),
    loading: loading || refreshing,
    error,
    loadOlder,
  };
  return { readThread, history };
}
