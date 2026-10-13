'use client';

import Link from 'next/link';
import { useSearchParams } from 'next/navigation';
import { Suspense, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';

import { useAuth } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import type { AgentRunnerHostPool } from '@/lib/api/admin';
import { getAgentRunnerHosts, setAgentHostSettings, setAgentRuntimeVersion } from '@/lib/api/admin';
import type { SandboxArchiveSettings } from '@/lib/api/adminArchiveSettings';
import {
  getSandboxArchiveSettings,
  updateSandboxArchiveSettings,
} from '@/lib/api/adminArchiveSettings';
import { APIError, getErrorMessage } from '@/lib/utils/errors';

import AgentVersions, { agentName } from './AgentVersions';
import {
  ApplicationDeploymentSetup,
  ApplicationSettingsCategory,
  ApplicationSettingsFeedback,
  ApplicationSettingsHeader,
  categoryTabId,
  useApplicationSettings,
} from './ApplicationSettings';
import CloudLabSettings from './CloudLabSettings';
import SandboxArchiveSettingsCard from './SandboxArchiveSettings';
import { SettingsSearch } from './SettingsSearchPanel';
import { applicationCategoryName } from './settingsOrganization';
import { buildSettingsSearchItems, type SettingsSearchItem } from './settingsSearchIndex';
import { AdminNavigation } from '../AdminNavigation';
import { ADMIN_PAGE, AdminPageHeader } from '../AdminPageHeader';
import { GlobalSettingsCard, type NamedHost } from '../hosts/HostSettings';
import { runtimeMenu, serverInventory } from '../hosts/format';

const OTHER_SETTINGS_TABS = [
  { id: 'versions', label: 'Agent versions' },
  { id: 'sandbox', label: 'Sandbox' },
  { id: 'cloudlab', label: 'CloudLab' },
] as const;

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
  const content = useRef<HTMLDivElement>(null);
  const [searchTarget, setSearchTarget] = useState<string | null>(null);
  const [cloudLabVisited, setCloudLabVisited] = useState(queryTab === 'cloudlab');
  const tabButtons = useRef<(HTMLButtonElement | null)[]>([]);
  const focusedTab = useRef<HTMLButtonElement | null>(null);
  const [pool, setPool] = useState<AgentRunnerHostPool | null>(null);
  const [archiveSettings, setArchiveSettings] = useState<SandboxArchiveSettings | null>(null);
  const [archiveEditorReset, setArchiveEditorReset] = useState(0);
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

  const selectUrlTab = useCallback((tab: string) => {
    const url = new URL(window.location.href);
    if (tab === 'application') url.searchParams.delete('tab');
    else url.searchParams.set('tab', tab);
    window.history.replaceState(null, '', `${url.pathname}${url.search}${url.hash}`);
  }, []);
  const revealCategory = useCallback(
    (category: string) =>
      selectUrlTab(category === 'Sandbox archives' ? 'sandbox' : categoryTabId(category)),
    [selectUrlTab],
  );

  const fail = useCallback((cause: unknown) => {
    if (cause instanceof APIError && (cause.statusCode === 401 || cause.statusCode === 403)) {
      revoked.current = true;
      request.current?.abort();
      sequence.current += 1;
      setPool(null);
      setArchiveSettings(null);
      setNotice(null);
      setError(null);
      setDenied(true);
    } else {
      setError(getErrorMessage(cause));
    }
  }, []);

  const applicationSettings = useApplicationSettings({
    onAccessDenied: fail,
    onRevealCategory: revealCategory,
    enabled: !denied,
  });
  const applicationCategories = [...applicationSettings.groups.keys()].filter(
    (category) => category !== 'Sandbox archives',
  );
  const settingsTabs = [
    ...(applicationCategories.length
      ? applicationCategories.map((category) => ({ id: categoryTabId(category), label: category }))
      : [{ id: 'application', label: 'Application' }]),
    ...OTHER_SETTINGS_TABS,
  ];
  // Preserve a requested category in the URL while its definitions are loading.
  // Missing, legacy, or unknown tabs display the first available category.
  let requestedTab = queryTab;
  if (queryTab?.startsWith('application-')) {
    try {
      const category = applicationCategoryName(decodeURIComponent(queryTab.slice(12)));
      requestedTab = category === 'Sandbox archives' ? 'sandbox' : categoryTabId(category);
    } catch {
      // A malformed category link falls back to the first available page.
    }
  }
  const selectedTab = settingsTabs.find(({ id }) => id === requestedTab)?.id ?? settingsTabs[0].id;
  const activeCategory = applicationCategories.find(
    (category) => categoryTabId(category) === selectedTab,
  );
  const showApplication = selectedTab === 'application' || activeCategory !== undefined;
  const selectedTabIndex = settingsTabs.findIndex(({ id }) => id === selectedTab);

  useLayoutEffect(() => {
    if (focusedTab.current && !focusedTab.current.isConnected) {
      tabButtons.current[selectedTabIndex]?.focus();
    }
  });

  useEffect(() => {
    const button = tabButtons.current[selectedTabIndex];
    const navigation = button?.parentElement;
    if (button && navigation) {
      const bounds = button.getBoundingClientRect();
      const viewport = navigation.getBoundingClientRect();
      // Reveal horizontally clipped tabs without undoing a field link's vertical scroll.
      if (bounds.left < viewport.left) navigation.scrollLeft += bounds.left - viewport.left;
      else if (bounds.right > viewport.right)
        navigation.scrollLeft += bounds.right - viewport.right;
    }
    if (selectedTab === 'cloudlab') setCloudLabVisited(true);
  }, [selectedTab, selectedTabIndex]);

  const selectTab = (tab: string) => {
    setSearchTarget(null);
    const category = applicationCategories.find((name) => categoryTabId(name) === tab);
    if (category) applicationSettings.selectCategory(category);
    selectUrlTab(tab);
  };

  const load = useCallback(async () => {
    if (writing.current || revoked.current) return;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    const current = ++sequence.current;
    try {
      const [nextPool, nextArchiveSettings] = await Promise.all([
        getAgentRunnerHosts(controller.signal),
        getSandboxArchiveSettings(controller.signal),
      ]);
      if (!mounted.current || controller.signal.aborted || current !== sequence.current) return;
      setPool(nextPool);
      setArchiveSettings(nextArchiveSettings);
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

  const save = async <Result,>(
    action: () => Promise<Result>,
    apply: (next: Result) => void,
    message: string | null,
  ) => {
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
      apply(next);
      setNotice(message);
    } catch (cause) {
      if (mounted.current) fail(cause);
    } finally {
      writing.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  const servers: NamedHost[] = serverInventory(
    pool?.hosts ?? [],
    pool?.execution_profile ?? 'untrusted-kata-v1',
  ).flatMap(({ name, host }) => (host && host.status !== 'disabled' ? [{ name, host }] : []));
  const definitions = pool?.host_setting_definitions ?? [];
  const showHostStatus = selectedTab === 'versions' || selectedTab === 'sandbox';
  const versionMenu = runtimeMenu(servers.map(({ host }) => host));
  const searchItems = buildSettingsSearchItems({
    snapshot: applicationSettings.snapshot,
    hostDefinitions: definitions,
    runtimes: [
      ...new Set([
        ...versionMenu.map(([runtime]) => runtime),
        ...Object.keys(pool?.runtime_versions ?? {}),
      ]),
    ],
    archiveAvailable: archiveSettings !== null,
    applicationTab: applicationCategories.length
      ? categoryTabId(applicationCategories[0])
      : 'application',
  });

  const selectSearchResult = (item: SettingsSearchItem) => {
    if (item.applicationKey) {
      setSearchTarget(null);
      applicationSettings.revealSetting(item.applicationKey);
    } else {
      selectTab(item.tab);
      setSearchTarget(item.targetId);
    }
  };

  useEffect(() => {
    if (!searchTarget || !content.current || denied) return;
    const root = content.current;
    const focusTarget = () => {
      const target = document.getElementById(searchTarget);
      if (!target || !root.contains(target) || target.closest('[hidden]')) return false;
      for (let ancestor = target.parentElement; ancestor; ancestor = ancestor.parentElement) {
        if (ancestor instanceof HTMLDetailsElement) ancestor.open = true;
      }
      let control = target.matches('input, select, textarea, button, [tabindex]')
        ? target
        : target.querySelector<HTMLElement>('input, select, textarea, button');
      if (control?.matches(':disabled')) {
        control = control.closest<HTMLElement>('label, form') ?? target;
        control.tabIndex = -1;
      }
      (control ?? target).focus();
      target.scrollIntoView?.({ block: 'center' });
      setSearchTarget(null);
      return true;
    };
    if (focusTarget()) return;
    // CloudLab fields arrive after its first visit; retain the target while loading.
    const observer = new MutationObserver(() => {
      if (focusTarget()) observer.disconnect();
    });
    observer.observe(root, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [searchTarget, selectedTab, denied]);

  if (denied) return <AccessDenied />;

  return (
    <div ref={content} className="min-w-0 flex-1 overflow-y-auto">
      <section className={ADMIN_PAGE}>
        <AdminPageHeader
          section="Settings"
          description="Configure application defaults, agent versions, and sandbox behavior."
        />
        <AdminNavigation current="settings" />
        <>
          <p className="mt-6 max-w-2xl text-sm leading-6 text-muted">
            Override sandbox settings for one server in{' '}
            <Link
              href="/admin/hosts"
              className="font-medium text-accent underline underline-offset-2"
            >
              Servers
            </Link>
            .
          </p>
          <SettingsSearch
            items={searchItems}
            loading={loading || applicationSettings.loading}
            onInteract={() => setSearchTarget(null)}
            onSelect={selectSearchResult}
          />
          <div
            role="tablist"
            aria-label="Settings sections"
            className="mt-6 flex w-full gap-1 overflow-x-auto rounded-xl border border-border bg-surface-muted p-1 sm:flex-wrap sm:overflow-x-visible"
          >
            {settingsTabs.map(({ id, label }, index) => (
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
                onFocus={(event) => {
                  focusedTab.current = event.currentTarget;
                }}
                onBlur={() => {
                  focusedTab.current = null;
                }}
                onClick={() => selectTab(id)}
                onKeyDown={(event) => {
                  const nextTabs: Partial<Record<string, number>> = {
                    ArrowRight: (index + 1) % settingsTabs.length,
                    ArrowLeft: (index - 1 + settingsTabs.length) % settingsTabs.length,
                    Home: 0,
                    End: settingsTabs.length - 1,
                  };
                  const nextIndex = nextTabs[event.key];
                  if (nextIndex === undefined) return;
                  event.preventDefault();
                  selectTab(settingsTabs[nextIndex].id);
                  tabButtons.current[nextIndex]?.focus();
                }}
                className={`min-h-11 shrink-0 rounded-lg px-4 py-2 text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson dark:focus-visible:ring-crimson-light ${
                  selectedTab === id
                    ? 'bg-surface text-foreground shadow-subtle'
                    : 'text-muted hover:bg-surface-hover hover:text-foreground'
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
          <section
            role={applicationCategories.length ? undefined : 'tabpanel'}
            id={applicationCategories.length ? undefined : 'admin-settings-panel-application'}
            aria-labelledby={
              applicationCategories.length ? undefined : 'admin-settings-tab-application'
            }
            hidden={!showApplication}
            tabIndex={applicationCategories.length ? undefined : 0}
            className="ui-card mt-6 p-4 sm:p-6"
            aria-label="Application settings"
          >
            <ApplicationSettingsHeader settings={applicationSettings} title={activeCategory} />
            <ApplicationSettingsFeedback settings={applicationSettings} />
            {applicationCategories.map((category) => {
              const id = categoryTabId(category);
              return (
                <div
                  key={category}
                  role="tabpanel"
                  id={`admin-settings-panel-${id}`}
                  aria-labelledby={`admin-settings-tab-${id}`}
                  hidden={selectedTab !== id}
                  tabIndex={0}
                  className="mt-6"
                >
                  <ApplicationSettingsCategory
                    settings={applicationSettings}
                    category={category}
                    active={selectedTab === id}
                    showHeading={false}
                  />
                </div>
              );
            })}
            {applicationSettings.snapshot && applicationCategories.length === 0 && (
              <p className="mt-5 rounded-xl bg-surface-muted p-4 text-sm text-muted">
                No application settings are available.
              </p>
            )}
            <ApplicationDeploymentSetup settings={applicationSettings} />
          </section>
          <div
            role="tabpanel"
            id="admin-settings-panel-versions"
            aria-labelledby="admin-settings-tab-versions"
            hidden={selectedTab !== 'versions'}
            tabIndex={0}
          >
            {pool && (
              <AgentVersions
                menu={versionMenu}
                servers={servers}
                selected={pool.runtime_versions ?? {}}
                busy={busy}
                onChange={(runtime, version) =>
                  void save(
                    () => setAgentRuntimeVersion(runtime, version || null),
                    setPool,
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
            {(applicationSettings.groups.has('Sandbox archives') ||
              (!applicationSettings.snapshot &&
                (applicationSettings.loading || applicationSettings.error))) && (
              <section
                className="ui-card mt-6 p-4 sm:p-6"
                aria-label="Deletion archive destination"
              >
                <ApplicationSettingsHeader
                  settings={applicationSettings}
                  title="Deletion archive destination"
                />
                <ApplicationSettingsFeedback settings={applicationSettings} />
                {applicationSettings.groups.has('Sandbox archives') && (
                  <ApplicationSettingsCategory
                    settings={applicationSettings}
                    category="Sandbox archives"
                    active={selectedTab === 'sandbox'}
                    showHeading={false}
                  />
                )}
              </section>
            )}
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
                    void save(() => setAgentHostSettings({ [key]: value }), setPool, null)
                  }
                />
              ) : (
                <p className="mt-8 text-sm text-gray-500 dark:text-gray-400">
                  No sandbox settings are available.
                </p>
              ))}
            {archiveSettings && (
              <SandboxArchiveSettingsCard
                key={archiveEditorReset}
                settings={archiveSettings}
                hosts={servers.map(({ host }) => host)}
                busy={busy}
                onSave={(update) =>
                  void save(
                    () => updateSandboxArchiveSettings(update),
                    (next) => {
                      setArchiveSettings(next);
                      // A successful idempotent retry may keep the same DB
                      // revision. Reset the editor explicitly instead of
                      // relying on a revision-key remount.
                      setArchiveEditorReset((current) => current + 1);
                    },
                    update.enabled !== archiveSettings.enabled
                      ? update.enabled
                        ? 'Sandbox archives enabled.'
                        : 'Sandbox archives disabled.'
                      : 'Sandbox archive settings saved.',
                  )
                }
              />
            )}
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
        </>
      </section>
    </div>
  );
}
