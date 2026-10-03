'use client';

import { useEffect, useState } from 'react';
import { getAgentMessageRequest, type AgentMessageRequestApi } from '@/lib/api/agents';
import type { TrajectoryStep } from './trajectorySteps';

/** Read only the selected message; late gateway writes get bounded retries. */
export function useMessageRequest(step: TrajectoryStep, enabled: boolean) {
  const { jobId, attemptId, nativeMessageId, status, streaming } = step;
  const key = nativeMessageId ? `${jobId}:${attemptId}:${nativeMessageId}` : null;
  const [version, setVersion] = useState(0);
  const [loaded, setLoaded] = useState<{ key: string; detail: AgentMessageRequestApi } | null>(
    null,
  );
  const [loadingKey, setLoadingKey] = useState<string | null>(null);

  useEffect(() => {
    if (!enabled || !key || !nativeMessageId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    let retries = 0;
    setLoadingKey(key);
    const read = async () => {
      try {
        const detail = await getAgentMessageRequest(
          jobId,
          attemptId,
          nativeMessageId,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setLoaded({ key, detail });
        if (detail.status === 'not_recorded' && retries < 3) {
          timer = setTimeout(read, [1000, 3000, 8000][retries++]);
        }
      } catch {
        if (!controller.signal.aborted) {
          setLoaded({
            key,
            detail: { status: 'unavailable', request: null, reason: 'request_failed' },
          });
        }
      } finally {
        if (!controller.signal.aborted) setLoadingKey(null);
      }
    };
    void read();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [jobId, attemptId, nativeMessageId, key, status, streaming, enabled, version]);

  return {
    detail: key && loaded?.key === key ? loaded.detail : null,
    loading: key !== null && loadingKey === key,
    retry: () => setVersion((value) => value + 1),
  };
}
