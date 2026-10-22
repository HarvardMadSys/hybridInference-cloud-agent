'use client';

import { useEffect, useRef, type RefObject } from 'react';

import type { AdminSandboxState } from '@/lib/api/adminSandboxes';
import { getErrorMessage } from '@/lib/utils/errors';

import { accessDenied } from '../runs/shared';
import { readInventory, type SandboxInventorySnapshot } from './inventorySync';

interface Options {
  revision: number;
  stateFilter: AdminSandboxState | 'all';
  includeArchived: boolean;
  denied: boolean;
  denyAccess: () => void;
  autoRefreshRef: RefObject<boolean>;
  batchBusyRef: RefObject<boolean>;
  setLoading: (value: boolean) => void;
  setError: (value: string | null) => void;
  setSnapshot: (value: SandboxInventorySnapshot | null) => void;
}

export function useSandboxInventory(options: Options) {
  const {
    revision,
    stateFilter,
    includeArchived,
    denied,
    denyAccess,
    autoRefreshRef,
    batchBusyRef,
    setLoading,
    setError,
    setSnapshot,
  } = options;
  const cached = useRef<{ scope: string; snapshot: SandboxInventorySnapshot | null }>({
    scope: '',
    snapshot: null,
  });
  useEffect(() => {
    if (denied) return;
    const scope = `${stateFilter}:${includeArchived}`;
    if (cached.current.scope !== scope) cached.current = { scope, snapshot: null };
    let disposed = false;
    let accessRevoked = false;
    let request: AbortController | null = null;

    function unavailable() {
      return Boolean(
        request || accessRevoked || disposed || batchBusyRef.current || document.hidden,
      );
    }

    async function load() {
      if (unavailable()) return;
      const controller = new AbortController();
      request = controller;
      setLoading(true);
      setError(null);
      try {
        const snapshot = await readInventory(
          {
            limit: 200,
            ...(stateFilter !== 'running' ? { state: stateFilter } : {}),
            ...(includeArchived ? { include_archived: true } : {}),
          },
          cached.current.snapshot,
          controller.signal,
        );
        if (!disposed && !controller.signal.aborted && !batchBusyRef.current) {
          cached.current = { scope, snapshot };
          setSnapshot(snapshot);
        }
      } catch (error) {
        if (disposed || controller.signal.aborted) return;
        if (accessDenied(error)) {
          accessRevoked = true;
          cached.current.snapshot = null;
          denyAccess();
        } else setError(getErrorMessage(error));
      } finally {
        if (!disposed) setLoading(false);
        request = null;
      }
    }

    function visibilityChanged() {
      if (document.hidden) request?.abort();
      else if (autoRefreshRef.current) void load();
    }
    void load();
    const timer = setInterval(() => {
      if (autoRefreshRef.current && !document.hidden) void load();
    }, 30_000);
    document.addEventListener('visibilitychange', visibilityChanged);
    return () => {
      disposed = true;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', visibilityChanged);
      request?.abort();
    };
  }, [
    revision,
    stateFilter,
    includeArchived,
    denied,
    denyAccess,
    autoRefreshRef,
    batchBusyRef,
    setLoading,
    setError,
    setSnapshot,
  ]);
}
