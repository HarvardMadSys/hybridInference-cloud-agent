'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import type { AgentRunnerHostPool } from '@/lib/api/admin';
import { getAgentRunnerHosts, setAgentHostSettings, setAgentRuntimeVersion } from '@/lib/api/admin';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import AgentVersions, { agentName } from './AgentVersions';
import ApplicationSettings from './ApplicationSettings';
import CloudLabSettings from './CloudLabSettings';
import { AdminNavigation } from '../AdminNavigation';
import { GlobalSettingsCard, type NamedHost } from '../hosts/HostSettings';
import { runtimeMenu, serverInventory } from '../hosts/format';

const SETTINGS_TABS = [
  { id: 'application', label: 'Application' },
  { id: 'versions', label: 'Agent versions' },
  { id: 'sandbox', label: 'Sandbox' },
  { id: 'cloudlab', label: 'CloudLab' },
] as const;

type SettingsTab = (typeof SETTINGS_TABS)[number]['id'];

export default function AdminSettingsPage() {
  const { state } = useAuth();
  if (!hasAgentPermission(state.user?.agent_access, 'agent.admin')) {
    return <AccessDenied />;
  }
  return (
    <Suspense>
      <SettingsView key={state.user?.id ?? 'admin'} />
    </Suspense>
  );
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
  const queryTab = useSearchParams().get('tab');
  const selectedTab: SettingsTab =
    SETTINGS_TABS.find(({ id }) => id === queryTab)?.id ?? 'application';
  const [cloudLabVisited, setCloudLabVisited] = useState(selectedTab === 'cloudlab');
  const tabButtons = useRef<(HTMLButtonElement | null)[]>([]);
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

  useEffect(() => {
    const index = SETTINGS_TABS.findIndex(({ id }) => id === selectedTab);
    tabButtons.current[index]?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' });
    if (selectedTab === 'cloudlab') setCloudLabVisited(true);
  }, [selectedTab]);

  const selectTab = (tab: SettingsTab) => {
    const url = new URL(window.location.href);
    if (tab === 'application') url.searchParams.delete('tab');
    else url.searchParams.set('tab', tab);
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  };

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

  const save = async (action: () => Promise<AgentRunnerHostPool>, message: string | null) => {
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

  const servers: NamedHost[] = serverInventory(
    pool?.hosts ?? [],
    pool?.execution_profile ?? 'untrusted-kata-v1',
  ).flatMap(({ name, host }) => (host && host.status !== 'disabled' ? [{ name, host }] : []));
  const definitions = pool?.host_setting_definitions ?? [];
  const showHostStatus = selectedTab === 'versions' || selectedTab === 'sandbox';

  return (
    <div className="flex-1 overflow-y-auto">
      <section className="mx-auto w-full max-w-7xl px-6 py-12 lg:px-12 lg:py-16">
        <h1 className="text-3xl font-semibold tracking-tight text-gray-900 dark:text-gray-50">
          Admin
        </h1>
        <AdminNavigation current="settings" />
        <div className="max-w-4xl">
          <p className="mt-6 text-sm text-gray-500 dark:text-gray-400">
            Application settings, default agent versions, sandbox settings, and CloudLab defaults.
            Override sandbox settings for one server in{' '}
            <Link
              href="/admin/hosts"
              className="underline hover:text-gray-700 dark:hover:text-gray-200"
            >
              Servers
            </Link>
            .
          </p>
          <div
            role="tablist"
            aria-label="Settings sections"
            className="mt-6 flex gap-1 overflow-x-auto rounded-lg bg-gray-100 p-1 dark:bg-white/5"
          >
            {SETTINGS_TABS.map(({ id, label }, index) => (
              <button
                key={id}
                ref={(element) => {
                  tabButtons.current[index] = element;
                }}
                type="button"
                role="tab"
                id={`admin-settings-tab-${id}`}
                aria-controls={`admin-settings-panel-${id}`}
                aria-selected={selectedTab === id}
                tabIndex={selectedTab === id ? 0 : -1}
                onClick={() => selectTab(id)}
                onKeyDown={(event) => {
                  const nextTabs: Partial<Record<string, number>> = {
                    ArrowRight: (index + 1) % SETTINGS_TABS.length,
                    ArrowLeft: (index - 1 + SETTINGS_TABS.length) % SETTINGS_TABS.length,
                    Home: 0,
                    End: SETTINGS_TABS.length - 1,
                  };
                  const nextIndex = nextTabs[event.key];
                  if (nextIndex === undefined) return;
                  event.preventDefault();
                  selectTab(SETTINGS_TABS[nextIndex].id);
                  tabButtons.current[nextIndex]?.focus();
                }}
                className={`shrink-0 rounded-md px-4 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson dark:focus-visible:ring-crimson-light ${
                  selectedTab === id
                    ? 'bg-white text-gray-900 shadow-sm dark:bg-gray-800 dark:text-gray-50'
                    : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-100'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
          {showHostStatus && error && (
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
          {showHostStatus && notice && (
            <p role="status" className="mt-6 text-sm text-gray-600 dark:text-gray-300">
              {notice}
            </p>
          )}
          {showHostStatus && loading && (
            <p role="status" className="mt-8 text-sm text-gray-500 dark:text-gray-400">
              Loading settings…
            </p>
          )}
          {/* Keep inactive panels mounted so switching tabs preserves unsaved edits. */}
          <div
            role="tabpanel"
            id="admin-settings-panel-application"
            aria-labelledby="admin-settings-tab-application"
            hidden={selectedTab !== 'application'}
            tabIndex={0}
          >
            <ApplicationSettings onAccessDenied={fail} />
          </div>
          <div
            role="tabpanel"
            id="admin-settings-panel-versions"
            aria-labelledby="admin-settings-tab-versions"
            hidden={selectedTab !== 'versions'}
            tabIndex={0}
          >
            {pool && (
              <AgentVersions
                menu={runtimeMenu(servers.map(({ host }) => host))}
                servers={servers}
                selected={pool.runtime_versions ?? {}}
                busy={busy}
                onChange={(runtime, version) =>
                  void save(
                    () => setAgentRuntimeVersion(runtime, version || null),
                    version
                      ? `New ${agentName(runtime)} sandboxes use ${version}. Existing sessions keep their version.`
                      : `New ${agentName(runtime)} sandboxes use each server's default version.`,
                  )
                }
              />
            )}
          </div>
          <div
            role="tabpanel"
            id="admin-settings-panel-sandbox"
            aria-labelledby="admin-settings-tab-sandbox"
            hidden={selectedTab !== 'sandbox'}
            tabIndex={0}
          >
            {pool &&
              (definitions.length > 0 ? (
                <GlobalSettingsCard
                  title="Sandbox"
                  definitions={definitions}
                  values={pool.host_settings ?? {}}
                  servers={servers}
                  busy={busy}
                  // No notice: each setting's own line says how far a saved
                  // value has reached, and keeps saying it as servers take it up.
                  onChange={(key, value) =>
                    void save(() => setAgentHostSettings({ [key]: value }), null)
                  }
                />
              ) : (
                <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">
                  No sandbox settings are available.
                </p>
              ))}
          </div>
          <div
            role="tabpanel"
            id="admin-settings-panel-cloudlab"
            aria-labelledby="admin-settings-tab-cloudlab"
            hidden={selectedTab !== 'cloudlab'}
            tabIndex={0}
          >
            {(cloudLabVisited || selectedTab === 'cloudlab') && (
              <CloudLabSettings onAccessDenied={fail} />
            )}
          </div>
        </div>
      </section>
    </div>
  );
}
