'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { AgentsSidebar } from '@/components/features/agents/AgentsSidebar';
import { useAuth, useT } from '@/components/providers';
import { hasRole } from '@/components/providers/AuthProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';

export const SIDEBAR_COLLAPSED_STORAGE_KEY = 'agents.sidebar.collapsed';

const NAV_ITEM =
  'rounded-md px-2.5 py-1.5 transition-colors hover:bg-gray-100 hover:text-gray-900 dark:hover:bg-white/10 dark:hover:text-gray-100';
const NAV_ITEM_CURRENT =
  'rounded-md bg-crimson/[0.06] px-2.5 py-1.5 text-crimson ring-1 ring-inset ring-crimson/10 dark:bg-crimson/20 dark:text-crimson-light dark:ring-crimson/25';

// App-mode shell (same fixed-overlay pattern as the playground): slim header +
// repo/task sidebar + main pane. Deep links keep working — /agents/* are real
// routes; only the marketing chrome is replaced.
function AgentsHeader({
  sidebarCollapsed,
  onToggleSidebar,
}: {
  sidebarCollapsed: boolean;
  onToggleSidebar: () => void;
}) {
  const router = useRouter();
  const pathname = usePathname() ?? '/';
  const { state, logout } = useAuth();
  const { branding, features } = useSiteConfig();
  const t = useT();

  // Which nav item is lit has to follow the route. The highlight used to be
  // painted on `Agents` unconditionally — a leftover from when this app had one
  // destination — so standing on the Hosts page read as "you are on Agents".
  //
  // Split by section rather than by exact path: a job page is still the task
  // app, and an `/admin/*` page added later without a nav entry of its own
  // lights neither item instead of wrongly lighting `Agents`.
  const onHosts = pathname.startsWith('/admin/hosts');
  const onTasks = !pathname.startsWith('/admin');

  const handleLogout = async () => {
    await logout();
    router.replace('/');
  };

  return (
    <header className="flex shrink-0 items-center justify-between border-b border-gray-200/80 bg-white/95 px-5 py-2.5 shadow-[0_1px_0_rgba(17,24,39,0.02)] backdrop-blur dark:border-white/10 dark:bg-gray-900/95">
      <div className="flex min-w-0 items-baseline gap-2">
        <button
          type="button"
          onClick={onToggleSidebar}
          aria-label={sidebarCollapsed ? t('nav.expandTaskList') : t('nav.collapseTaskList')}
          aria-expanded={!sidebarCollapsed}
          aria-controls="agents-sidebar"
          aria-keyshortcuts="Meta+B Control+B"
          title={sidebarCollapsed ? t('nav.expandTaskList') : t('nav.collapseTaskList')}
          className="-ml-1.5 self-center rounded-md p-1.5 text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/25 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100"
        >
          <svg
            aria-hidden="true"
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={1.8}
          >
            <rect x="3.5" y="4" width="17" height="16" rx="2" />
            <path d="M9.5 4v16" />
          </svg>
        </button>
        <Link
          href="/"
          className="text-[17px] font-semibold tracking-[-0.02em] text-gray-950 transition-colors hover:text-crimson dark:text-gray-50 dark:hover:text-crimson-light"
        >
          {branding.appName}
        </Link>
        {branding.orgName && (
          <a
            href={branding.orgUrl}
            className="font-serif text-[13px] tracking-[0.01em] text-gray-500 transition-colors hover:text-crimson dark:text-gray-400 dark:hover:text-crimson-light"
            target="_blank"
            rel="noopener noreferrer"
          >
            {branding.orgName}
          </a>
        )}
      </div>
      <nav
        aria-label={t('nav.aria')}
        className="flex items-center gap-0.5 text-[13px] font-medium text-gray-600 dark:text-gray-400"
      >
        {/* Guarded like every other external link here, and for a sharper
            reason than tidiness: an anchor with an empty `href` resolves to the
            current document, so with `target="_blank"` an unconfigured status
            URL opened a second tab showing the page you were already on. A
            deployment with no status page should have no Status link. */}
        {branding.statusUrl && (
          <a
            href={branding.statusUrl}
            className={NAV_ITEM}
            target="_blank"
            rel="noopener noreferrer"
          >
            {t('nav.status')}
          </a>
        )}
        {/* Docs Assistant and Dashboard are the gateway's pages, not ours. In
            the monorepo they were same-origin routes; here they are absolute
            links to the gateway, and they disappear on a deployment that has
            not been told where the gateway is. */}
        {features.rag && branding.gatewayUrl && (
          <a href={`${branding.gatewayUrl}/chat`} className={NAV_ITEM}>
            {t('nav.docsAssistant')}
          </a>
        )}
        <Link
          href="/"
          className={onTasks ? NAV_ITEM_CURRENT : NAV_ITEM}
          aria-current={onTasks ? 'page' : undefined}
        >
          {t('nav.agents')}
        </Link>
        {/* This service's own operations page, so it is a route rather than a
            link back to the gateway. Admin-only, matching the endpoints it
            calls — the guard on the page is the one that counts; hiding the
            link only keeps it out of the way of everyone else. */}
        {hasRole(state.user?.role, 'admin') && (
          <Link
            href="/admin/hosts"
            className={onHosts ? NAV_ITEM_CURRENT : NAV_ITEM}
            aria-current={onHosts ? 'page' : undefined}
          >
            {t('nav.admin')}
          </Link>
        )}
        {branding.gatewayUrl && (
          <a href={`${branding.gatewayUrl}/dashboard`} className={NAV_ITEM}>
            {t('nav.dashboard')}
          </a>
        )}
        <button type="button" onClick={handleLogout} className={NAV_ITEM}>
          {t('nav.logOut')}
        </button>
      </nav>
    </header>
  );
}

function AgentsShell({
  children,
  sidebarCollapsed,
}: {
  children: React.ReactNode;
  sidebarCollapsed: boolean;
}) {
  const { state } = useAuth();
  const { branding } = useSiteConfig();
  const t = useT();

  // Admin-only: the same bar `require_agent_owner` enforces server-side. The
  // two must agree — a UI that lets someone in only to 403 every request they
  // make is worse than a clear refusal. Raised from 'internal' on 2026-09-04.
  if (!hasRole(state.user?.role, 'admin')) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
          {t('shell.internalRequired.title')}
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400">
          {t('shell.internalRequired.body')}
        </p>
        {branding.gatewayUrl && (
          <a
            href={`${branding.gatewayUrl}/dashboard`}
            className="text-sm font-medium text-crimson hover:underline dark:text-crimson-light"
          >
            {t('shell.internalRequired.back')}
          </a>
        )}
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1">
      <AgentsSidebar collapsed={sidebarCollapsed} />
      <main className="min-w-0 flex-1 overflow-y-auto bg-gray-50/40 dark:bg-gray-950">
        {children}
      </main>
    </div>
  );
}

export default function AgentsLayout({ children }: { children: React.ReactNode }) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  const toggleSidebar = useCallback(() => {
    setSidebarCollapsed((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, next ? '1' : '0');
      } catch {
        // Non-persisted collapse still works for this session.
      }
      return next;
    });
  }, []);

  // Read after mount so the server-rendered shell and the first client render
  // agree; the sidebar then folds away on the same frame as hydration.
  useEffect(() => {
    try {
      setSidebarCollapsed(window.localStorage.getItem(SIDEBAR_COLLAPSED_STORAGE_KEY) === '1');
    } catch {
      // Blocked storage just means the sidebar starts open.
    }
  }, []);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      const hasOnePrimaryModifier = event.metaKey !== event.ctrlKey;
      if (
        event.key.toLowerCase() !== 'b' ||
        !hasOnePrimaryModifier ||
        event.altKey ||
        event.shiftKey
      ) {
        return;
      }

      event.preventDefault();
      if (!event.repeat) {
        toggleSidebar();
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [toggleSidebar]);

  return (
    <ProtectedRoute>
      <div className="fixed inset-0 z-50 flex flex-col bg-gray-50 dark:bg-gray-950">
        <AgentsHeader sidebarCollapsed={sidebarCollapsed} onToggleSidebar={toggleSidebar} />
        <AgentsShell sidebarCollapsed={sidebarCollapsed}>{children}</AgentsShell>
      </div>
    </ProtectedRoute>
  );
}
