'use client';

import Link from 'next/link';
import { useCallback, useEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import type { AgentRunnerHostPool, HostSettingDefinition } from '@/lib/api/admin';
import { getAgentRunnerHosts, setAgentHostSettings, setAgentRuntimeVersion } from '@/lib/api/admin';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import AgentVersions, { agentName } from './AgentVersions';
import { AdminNavigation } from '../AdminNavigation';
import { GlobalSettingsCard } from '../hosts/HostSettings';
import { runtimeMenu, serverInventory } from '../hosts/format';

const SETTING_GROUPS = [
  { title: 'Task limits', keys: ['agent_timeout_s', 'source_import_max_mib'] },
  { title: 'Sandboxes', keys: ['chat_idle_timeout_s', 'shared_resident_capacity'] },
  {
    title: 'Terminals',
    keys: ['terminal_idle_ttl_s', 'terminal_hard_ttl_s', 'terminal_max_sessions'],
  },
];

function groupSettings(definitions: HostSettingDefinition[]) {
  const groups = SETTING_GROUPS.map(({ title, keys }) => ({
    title,
    definitions: definitions.filter((definition) => keys.includes(definition.key)),
  }));
  const known = new Set(SETTING_GROUPS.flatMap(({ keys }) => keys));
  groups.push({
    title: 'Other settings',
    definitions: definitions.filter(({ key }) => !known.has(key)),
  });
  return groups.filter((group) => group.definitions.length > 0);
}

export default function AdminSettingsPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) {
    return <AccessDenied />;
  }
  return <SettingsView key={state.user?.id ?? 'admin'} />;
}

function AccessDenied() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-3 p-8">
      <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-50">
        Admin access required
      </h1>
      <p className="text-sm text-gray-500 dark:text-gray-400">
        Settings are visible to admins only.
      </p>
    </div>
  );
}

function SettingsView() {
  const [pool, setPool] = useState<AgentRunnerHostPool | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [denied, setDenied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const mounted = useRef(true);
  const writing = useRef(false);
  const revoked = useRef(false);
  const request = useRef<AbortController | null>(null);
  const sequence = useRef(0);

  const fail = useCallback((cause: unknown) => {
    if (cause instanceof APIError && (cause.statusCode === 401 || cause.statusCode === 403)) {
      revoked.current = true;
      request.current?.abort();
      sequence.current += 1;
      setPool(null);
      setNotice(null);
      setError(null);
      setDenied(true);
    } else {
      setError(getErrorMessage(cause));
    }
  }, []);

  const load = useCallback(async () => {
    if (writing.current || revoked.current) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const current = ++sequence.current;
    try {
      const next = await getAgentRunnerHosts(controller.signal);
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      setPool(next);
      setError(null);
    } catch (cause) {
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      fail(cause);
    } finally {
      if (mounted.current && current === sequence.current) setLoading(false);
    }
  }, [fail]);

  useEffect(() => {
    mounted.current = true;
    void load();
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, 30_000);
    return () => {
      mounted.current = false;
      request.current?.abort();
      clearInterval(timer);
    };
  }, [load]);

  const save = async (action: () => Promise<AgentRunnerHostPool>, message: string) => {
    if (writing.current || revoked.current) return;
    writing.current = true;
    setBusy(true);
    setError(null);
    setNotice(null);
    // A poll started before a save must never restore the old value afterwards.
    request.current?.abort();
    sequence.current += 1;
    try {
      const next = await action();
      if (!mounted.current) return;
      setPool(next);
      setNotice(message);
    } catch (cause) {
      if (mounted.current) fail(cause);
    } finally {
      writing.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  if (denied) return <AccessDenied />;

  const hosts = serverInventory(
    pool?.hosts ?? [],
    pool?.execution_profile ?? 'untrusted-kata-v1',
  ).flatMap(({ host }) => (host && host.status !== 'disabled' ? [host] : []));
  const definitions = pool?.host_setting_definitions ?? [];

  return (
    <div className="flex-1 overflow-y-auto">
      <section className="mx-auto w-full max-w-7xl px-6 py-12 lg:px-12 lg:py-16">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="settings" />
        <div className="max-w-4xl">
          <p className="mt-6 text-sm text-gray-500 dark:text-gray-400">
            Default agent versions and limits for all servers. Set limits for an individual server
            in{' '}
            <Link
              href="/admin/hosts"
              className="underline hover:text-gray-700 dark:hover:text-gray-200"
            >
              Servers
            </Link>
            .
          </p>
          {error && (
            <div
              role="alert"
              className="mt-6 flex items-center justify-between gap-4 rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700 dark:border-red-500/25 dark:bg-red-500/10 dark:text-red-400"
            >
              <span>{error}</span>
              <button
                type="button"
                onClick={() => void load()}
                disabled={busy}
                className="shrink-0 font-medium underline disabled:opacity-40"
              >
                Retry
              </button>
            </div>
          )}
          {notice && (
            <p role="status" className="mt-6 text-sm text-gray-600 dark:text-gray-300">
              {notice}
            </p>
          )}
          {loading && (
            <p role="status" className="mt-8 text-sm text-gray-500 dark:text-gray-400">
              Loading settings…
            </p>
          )}
          {pool && (
            <>
              <AgentVersions
                menu={runtimeMenu(hosts)}
                selected={pool.runtime_versions ?? {}}
                busy={busy}
                onChange={(runtime, version) =>
                  void save(
                    () => setAgentRuntimeVersion(runtime, version || null),
                    `${agentName(runtime)} preference saved for new sandboxes. Existing chats keep their version.`,
                  )
                }
              />
              {groupSettings(definitions).map(({ title, definitions: group }) => (
                <GlobalSettingsCard
                  key={title}
                  title={title}
                  definitions={group}
                  values={pool.host_settings ?? {}}
                  hosts={hosts}
                  busy={busy}
                  onChange={(key, value) =>
                    void save(
                      () => setAgentHostSettings({ [key]: value }),
                      `${definitions.find((definition) => definition.key === key)?.label ?? 'Setting'} saved. Server application status is shown below.`,
                    )
                  }
                />
              ))}
            </>
          )}
        </div>
      </section>
    </div>
  );
}
