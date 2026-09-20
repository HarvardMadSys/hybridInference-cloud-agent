'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getMcpConnectionCapabilities,
  listMcpConnections,
  type McpConnectionApi,
} from '@/lib/api/mcp';
import type { McpBindingInputApi } from '@/lib/api/agents';
import { getErrorMessage } from '@/lib/utils/errors';
import type { Translator } from '@/lib/i18n/translate';

/**
 * Which of the owner's MCP servers this task may call.
 *
 * A server is off until it is turned on here: `POST /v1/agent/jobs` treats an
 * omitted selection as none, because connecting a server and letting a task
 * call it are two different decisions.
 *
 * Turning one on names its tools explicitly at `confirm`. The control plane
 * denies any capability a binding does not list, and computes `enabled` from
 * whether anything is left non-denied — so a binding sent with empty policy
 * maps arrives bound and switched off, which would look like the toggle did
 * nothing. `confirm` rather than `allow` because the first call is the first
 * time the owner sees what the server actually wants to do.
 */
export interface McpChoice {
  connection: Pick<McpConnectionApi, 'id' | 'label'>;
  selected: boolean;
  toolCount: number;
  busy: boolean;
  error?: string;
  /**
   * The connection cannot serve a task at all, so it is offered no switch.
   *
   * Distinct from a failed attempt, which leaves the switch in place: signing
   * in elsewhere or testing the connection can make that one work, and a row
   * that reports a problem while removing the only control is a dead end.
   */
  blocked: boolean;
}

/** A connection that cannot serve a task, and why, in the owner's terms. */
function unusable(connection: McpConnectionApi, t: Translator): string | null {
  if (connection.enabled === false) return t('attach.mcpDisabled');
  if (connection.status === 'draft') return t('attach.mcpDraft');
  if (connection.status === 'error' || connection.error_kind !== 'none') {
    return connection.error_kind === 'needs_authorization'
      ? t('attach.mcpNeedsSignIn')
      : t('attach.mcpUnavailable');
  }
  return null;
}

export function useComposerMcp(t: Translator) {
  const [connections, setConnections] = useState<McpConnectionApi[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [chosen, setChosen] = useState<Map<number, McpBindingInputApi>>(new Map());
  const [busy, setBusy] = useState<Set<number>>(new Set());
  const [failures, setFailures] = useState<Map<number, string>>(new Map());
  const generation = useRef(0);

  const reload = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const found = await listMcpConnections();
      if (generation.current === request) setConnections(found.connections);
    } catch (cause) {
      if (generation.current === request) setError(getErrorMessage(cause));
    } finally {
      if (generation.current === request) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void reload();
    return () => {
      generation.current += 1;
    };
  }, [reload]);

  const toggle = useCallback(
    async (connection: McpChoice['connection'], on: boolean) => {
      const id = connection.id;
      if (!on) {
        setChosen((old) => {
          const next = new Map(old);
          next.delete(id);
          return next;
        });
        return;
      }
      setBusy((old) => new Set(old).add(id));
      setFailures((old) => {
        const next = new Map(old);
        next.delete(id);
        return next;
      });
      try {
        const capabilities = await getMcpConnectionCapabilities(id);
        const tools = Object.fromEntries(
          capabilities
            .filter((capability) => capability.kind === 'tool')
            .map((capability) => [capability.name, 'confirm' as const]),
        );
        if (Object.keys(tools).length === 0) {
          // Binding a server with nothing to call would arrive disabled, which
          // reads as a toggle that did not take.
          throw new Error(t('attach.mcpNoTools'));
        }
        setChosen((old) => new Map(old).set(id, { connection_id: id, tools, enabled: true }));
      } catch (cause) {
        setFailures((old) => new Map(old).set(id, getErrorMessage(cause)));
      } finally {
        setBusy((old) => {
          const next = new Set(old);
          next.delete(id);
          return next;
        });
      }
    },
    [t],
  );

  const clear = useCallback(() => setChosen(new Map()), []);

  const choices: McpChoice[] = connections.map((connection) => {
    const structural = unusable(connection, t);
    return {
      connection,
      selected: chosen.has(connection.id),
      toolCount:
        Object.keys(chosen.get(connection.id)?.tools ?? {}).length || connection.tool_count,
      busy: busy.has(connection.id),
      error: failures.get(connection.id) ?? structural ?? undefined,
      blocked: structural !== null,
    };
  });

  return {
    choices,
    loading,
    error,
    reload,
    toggle,
    clear,
    count: chosen.size,
    /** What the create request sends. Empty means the task gets no servers. */
    bindings: [...chosen.values()],
  };
}
