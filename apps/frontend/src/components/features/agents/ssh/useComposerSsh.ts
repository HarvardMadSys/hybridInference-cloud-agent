'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getSshThreadBindings,
  listSshServers,
  replaceSshThreadBindings,
  type SshBindingApi,
  type SshServerApi,
} from '@/lib/api/sshServers';
import { getErrorMessage } from '@/lib/utils/errors';

/**
 * One server in the composer's SSH list.
 *
 * Only a server whose check passed can be turned on: the control plane
 * refuses any other, because nothing has confirmed its host key or that the
 * platform's key logs in.
 */
export interface SshChoice {
  id: string;
  name: string;
  /** `user@address:port`, as the agent will log in. */
  login: string;
  selected: boolean;
  /** Not checked yet: shown, but offered no switch. */
  blocked: boolean;
}

export interface ComposerSsh {
  choices: SshChoice[];
  loading: boolean;
  saving: boolean;
  error: string | null;
  count: number;
  reload: () => void;
  toggle: (id: string, on: boolean) => void;
}

function login(server: SshServerApi): string {
  return `${server.username}@${server.pinned_address ?? server.address}:${server.port}`;
}

/**
 * The owner's servers, read when the composer's menu asks for them rather
 * than on every composer mount: most chats never open the SSH list.
 */
function useServers() {
  const [servers, setServers] = useState<SshServerApi[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);

  const reload = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const found = await listSshServers();
      if (generation.current === request) setServers(found.servers);
    } catch (cause) {
      if (generation.current === request) setError(getErrorMessage(cause));
    } finally {
      if (generation.current === request) setLoading(false);
    }
  }, []);

  useEffect(
    () => () => {
      generation.current += 1;
    },
    [],
  );

  return { servers, loading, error, reload };
}

/** A new task: the selection is local until the create request carries it. */
export function useComposerSsh(): ComposerSsh & { serverIds: string[]; clear: () => void } {
  const { servers, loading, error, reload } = useServers();
  const [chosen, setChosen] = useState<Set<string>>(new Set());

  const toggle = useCallback((id: string, on: boolean) => {
    setChosen((old) => {
      const next = new Set(old);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  }, []);

  const clear = useCallback(() => setChosen(new Set()), []);
  const ready = new Set(servers.filter((s) => s.status === 'ready').map((s) => s.id));
  const serverIds = [...chosen].filter((id) => ready.has(id));

  return {
    choices: servers.map((server) => ({
      id: server.id,
      name: server.name,
      login: login(server),
      selected: chosen.has(server.id),
      blocked: server.status !== 'ready',
    })),
    loading,
    saving: false,
    error,
    count: serverIds.length,
    reload: () => void reload(),
    toggle,
    /** What the create request sends. Empty means the chat reaches no server. */
    serverIds,
    clear,
  };
}

/** An existing chat: each switch replaces its selection on the server, effective at once. */
export function useThreadComposerSsh(threadId?: string): ComposerSsh {
  const {
    servers,
    loading: serversLoading,
    error: serversError,
    reload: reloadServers,
  } = useServers();
  const [bindings, setBindings] = useState<SshBindingApi[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const operation = useRef(false);

  const reloadBindings = useCallback(async () => {
    if (!threadId || operation.current) return;
    const request = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const found = await getSshThreadBindings(threadId);
      if (request === generation.current) setBindings(found);
    } catch (cause) {
      if (request === generation.current) setError(getErrorMessage(cause));
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [threadId]);

  // A different chat starts empty; its selection is read when the menu opens.
  useEffect(() => {
    setBindings([]);
    setSaving(false);
    setError(null);
    operation.current = false;
    return () => {
      generation.current += 1;
    };
  }, [threadId]);

  const enabled = new Set(bindings.filter((b) => b.enabled).map((b) => b.server_id));

  const toggle = useCallback(
    async (id: string, on: boolean) => {
      if (!threadId || operation.current) return;
      operation.current = true;
      const request = ++generation.current;
      const next = new Set(bindings.filter((b) => b.enabled).map((b) => b.server_id));
      if (on) next.add(id);
      else next.delete(id);
      setLoading(false);
      setSaving(true);
      setError(null);
      try {
        const result = await replaceSshThreadBindings(threadId, [...next]);
        if (request === generation.current) setBindings(result);
      } catch (cause) {
        if (request === generation.current) setError(getErrorMessage(cause));
      } finally {
        if (request === generation.current) {
          operation.current = false;
          setSaving(false);
        }
      }
    },
    [bindings, threadId],
  );

  return {
    choices: servers.map((server) => ({
      id: server.id,
      name: server.name,
      login: login(server),
      selected: enabled.has(server.id),
      // A server that is on stays switchable, so it can always be turned off.
      blocked: server.status !== 'ready' && !enabled.has(server.id),
    })),
    loading: serversLoading || loading,
    saving,
    error: error ?? serversError,
    count: enabled.size,
    reload: () => {
      void reloadServers();
      void reloadBindings();
    },
    toggle: (id, on) => void toggle(id, on),
  };
}
