'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { getMcpThreadBindings, updateMcpThreadBinding, type McpBindingApi } from '@/lib/api/mcp';
import { getErrorMessage } from '@/lib/utils/errors';
import type { McpChoice } from './useComposerMcp';

/** Existing conversations edit their effective bindings, including account defaults. */
export function useThreadComposerMcp(threadId?: string) {
  const [bindings, setBindings] = useState<McpBindingApi[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const operation = useRef(false);

  const reload = useCallback(async () => {
    if (!threadId || operation.current) return;
    const request = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const result = await getMcpThreadBindings(threadId);
      if (request === generation.current) setBindings(result.bindings);
    } catch (cause) {
      if (request === generation.current) setError(getErrorMessage(cause));
    } finally {
      if (request === generation.current) setLoading(false);
    }
  }, [threadId]);

  useEffect(() => {
    setBindings([]);
    setSaving(false);
    operation.current = false;
    void reload();
    return () => {
      generation.current += 1;
    };
  }, [reload]);

  const toggle = useCallback(
    async (connection: McpChoice['connection'], enabled: boolean) => {
      const binding = bindings.find((row) => row.connection_id === connection.id);
      if (!threadId || !binding || operation.current) return;
      operation.current = true;
      const request = ++generation.current;
      setLoading(false);
      setSaving(true);
      setError(null);
      // Preserve custom permissions across off/on. An empty explicit selection
      // must return to ask-before-call defaults to make enabling it effective.
      const hasPermission = [binding.tools, binding.resources, binding.prompts].some((rules) =>
        Object.values(rules).some((policy) => policy !== 'deny'),
      );
      const resetToDefaults = enabled && !hasPermission && !binding.selection_pending;
      try {
        const result = await updateMcpThreadBinding(threadId, {
          connection_id: binding.connection_id,
          alias: binding.alias,
          enabled,
          use_defaults: binding.selection_pending || resetToDefaults,
          tools: resetToDefaults ? {} : binding.tools,
          resources: resetToDefaults ? {} : binding.resources,
          prompts: resetToDefaults ? {} : binding.prompts,
        });
        if (request === generation.current) setBindings(result.bindings);
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

  const choices: McpChoice[] = bindings.map((binding) => ({
    connection: { id: binding.connection_id, label: binding.connection_label },
    selected: binding.enabled,
    toolCount: binding.capabilities.filter((capability) => capability.kind === 'tool').length,
    busy: saving,
    error: binding.error ?? undefined,
    // Unavailable servers can still be disabled for this conversation.
    blocked: !binding.available && !binding.enabled,
  }));
  return {
    choices,
    loading,
    saving,
    error,
    reload,
    toggle,
    count: bindings.filter((binding) => binding.enabled).length,
  };
}
