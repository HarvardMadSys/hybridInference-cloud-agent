'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useCallback, useEffect, useState } from 'react';
import { ProtectedRoute } from '@/components/features/auth/ProtectedRoute';
import { AgentsSidebar } from '@/components/features/agents/AgentsSidebar';
import { useAgentChangeStream } from '@/components/features/agents/agentPush';
import { AgentNotifications } from '@/components/features/agents/AgentNotifications';
import { FirstRunProvider } from '@/components/features/agents/firstRun';
import { AgentReadProvider } from '@/components/features/agents/AgentReadProvider';
import { useAuth, useT } from '@/components/providers';
import { hasAgentPermission } from '@/components/providers/AuthProvider';
import { useSiteConfig } from '@/components/providers/SiteConfigProvider';
import { DeploymentFooter } from '@/components/DeploymentFooter';
import { McpApprovalsProvider } from '@/components/features/agents/mcp/McpApprovalsProvider';
import { McpApprovalsLink } from '@/components/features/agents/mcp/McpApprovalsView';

import { SIDEBAR_COLLAPSED_STORAGE_KEY } from './sidebarStorage';

const NAV_ITEM =
  'inline-flex min-h-9 shrink-0 items-center rounded-lg px-3 py-2 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/30 dark:hover:bg-white/10 dark:hover:text-gray-100';
const NAV_ITEM_CURRENT =
  'inline-flex min-h-9 shrink-0 items-center rounded-lg bg-crimson/[0.06] px-3 py-2 text-crimson ring-1 ring-inset ring-crimson/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/30 dark:bg-crimson/20 dark:text-crimson-light dark:ring-crimson/25';

// App-mode shell (same fixed-overlay pattern as the playground): slim header +
// repo/task sidebar + main pane. Deep links keep working — /agents/* are real
// routes; only the marketing chrome is replaced.
function AgentsHeader({ sidebarToggle }: { sidebarToggle?: React.ReactNode }) {
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
  const onAdmin = pathname.startsWith('/admin/');
  const onTasks = !pathname.startsWith('/admin') && pathname !== '/approvals';

  const handleLogout = async () => {
    await logout();
    router.replace('/');
  };

  return (
    <header className="flex shrink-0 flex-wrap items-center justify-between gap-x-4 gap-y-2 border-b border-gray-200/80 bg-white/95 px-4 py-3 shadow-[0_1px_0_rgba(17,24,39,0.02)] backdrop-blur sm:px-5 dark:border-white/10 dark:bg-gray-900/95">
      <div className="flex min-w-0 items-center gap-2.5">
        {sidebarToggle}
        <Link
          href="/"
          className="flex min-w-0 items-center gap-2.5 rounded-md text-[17px] font-semibold tracking-[-0.02em] text-gray-950 transition-colors hover:text-crimson focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/30 dark:text-gray-50 dark:hover:text-crimson-light"
        >
          <span
            aria-hidden="true"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-crimson text-white shadow-sm"
          >
            <svg
              className="h-5 w-5"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={1.8}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="m5 7 5 5-5 5m8 0h6" />
            </svg>
          </span>
          <span className="truncate">{branding.appName}</span>
        </Link>
        {branding.orgName && (
          <a
            href={branding.orgUrl}
            className="hidden font-serif text-[13px] tracking-[0.01em] text-gray-500 transition-colors hover:text-crimson sm:inline dark:text-gray-400 dark:hover:text-crimson-light"
            target="_blank"
            rel="noopener noreferrer"
          >
            {branding.orgName}
          </a>
        )}
      </div>
      <nav
        aria-label={t('nav.aria')}
        className="flex max-w-full items-center gap-1 overflow-x-auto text-[13px] font-medium text-gray-600 max-md:w-full dark:text-gray-400"
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
        {hasAgentPermission(state.user?.agent_access, 'agent.use') && (
          <McpApprovalsLink
            current={pathname === '/approvals'}
            className={pathname === '/approvals' ? NAV_ITEM_CURRENT : NAV_ITEM}
          />
        )}
        {/* This service's own operations page, so it is a route rather than a
            link back to the gateway. Admin-only, matching the endpoints it
            calls — the guard on the page is the one that counts; hiding the
            link only keeps it out of the way of everyone else. */}
        {hasAgentPermission(state.user?.agent_access, 'agent.admin') && (
          <Link
            href="/admin/hosts"
            className={onAdmin ? NAV_ITEM_CURRENT : NAV_ITEM}
            aria-current={onAdmin ? 'page' : undefined}
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

function SidebarToggleButton({
  sidebarCollapsed,
  onToggleSidebar,
}: {
  sidebarCollapsed: boolean;
  onToggleSidebar: () => void;
}) {
  const t = useT();
  return (
    <button
      id="agents-sidebar-toggle"
      type="button"
      onClick={onToggleSidebar}
      aria-label={sidebarCollapsed ? t('nav.expandTaskList') : t('nav.collapseTaskList')}
      aria-expanded={!sidebarCollapsed}
      aria-controls="agents-sidebar"
      aria-keyshortcuts="Meta+B Control+B"
      title={sidebarCollapsed ? t('nav.expandTaskList') : t('nav.collapseTaskList')}
      className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-gray-500 transition-colors hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-crimson/25 dark:text-gray-400 dark:hover:bg-white/10 dark:hover:text-gray-100"
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
  );
}

/** The owner's change stream, held once per tab for every list and page in it. */
function AgentChangeStream({ userId }: { userId: string | undefined }) {
  useAgentChangeStream(userId);
  return null;
}

function AgentsShell({
  children,
  sidebarCollapsed,
  sidebarToggle,
  mobileSidebarOpen,
  onSidebarNavigate,
}: {
  children: React.ReactNode;
  sidebarCollapsed: boolean;
  sidebarToggle?: React.ReactNode;
  mobileSidebarOpen: boolean;
  onSidebarNavigate: () => void;
}) {
  const { state } = useAuth();
  const { branding } = useSiteConfig();
  const t = useT();

  // The Gateway decides admission; account role is only profile metadata.
  if (!hasAgentPermission(state.user?.agent_access, 'agent.use')) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-3">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
          {t('shell.accessRequired.title')}
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400">{t('shell.accessRequired.body')}</p>
        {branding.gatewayUrl && (
          <a
            href={`${branding.gatewayUrl}/dashboard`}
            className="text-sm font-medium text-crimson hover:underline dark:text-crimson-light"
          >
            {t('shell.accessRequired.back')}
          </a>
        )}
      </div>
    );
  }

  // One first-run answer for the sidebar and every page, asked for here once
  // rather than by each of them. Keyed by account, so another one starts over.
  return (
    <FirstRunProvider key={state.user?.id} userId={state.user?.id}>
      <AgentReadProvider userId={state.user?.id}>
        <AgentChangeStream userId={state.user?.id} />
        <AgentNotifications />
        <div className="flex min-h-0 flex-1">
          <div
            className={`flex min-h-0 shrink-0 ${mobileSidebarOpen ? 'max-md:w-full max-md:[&>aside]:!w-full max-md:[&>[role=separator]]:hidden' : ''}`}
            onClick={(event) => {
              if (mobileSidebarOpen && (event.target as HTMLElement).closest('a[href]'))
                onSidebarNavigate();
            }}
          >
            <AgentsSidebar collapsed={sidebarCollapsed} headerAction={sidebarToggle} />
          </div>
          <main
            id="agents-workspace"
            hidden={mobileSidebarOpen}
            tabIndex={-1}
            className="min-w-0 flex-1 overflow-y-auto bg-white/70 outline-none dark:bg-gray-950"
          >
            {children}
          </main>
        </div>
      </AgentReadProvider>
    </FirstRunProvider>
  );
}

export default function AgentsLayout({ children }: { children: React.ReactNode }) {
  const { state } = useAuth();
  const pathname = usePathname();
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobile, setMobile] = useState(false);
  const [mobileSidebarOpen, setMobileSidebarOpen] = useState(false);

  const closeMobileSidebar = useCallback(() => {
    setMobileSidebarOpen(false);
    window.requestAnimationFrame(() => document.getElementById('agents-workspace')?.focus());
  }, []);

  const toggleSidebar = useCallback(() => {
    // A portaled modal stays visible when the workspace is hidden. Keep its
    // focus cycle intact instead of moving focus to the task list behind it.
    if (
      [...document.querySelectorAll('[aria-modal="true"], dialog[open]')].some(
        (dialog) => !dialog.closest('[hidden], [aria-hidden="true"]'),
      )
    ) {
      return;
    }
    if (mobile) {
      if (mobileSidebarOpen) {
        closeMobileSidebar();
        return;
      }
      setMobileSidebarOpen((current) => !current);
      return;
    }
    setSidebarCollapsed((current) => {
      const next = !current;
      try {
        window.localStorage.setItem(SIDEBAR_COLLAPSED_STORAGE_KEY, next ? '1' : '0');
      } catch {
        // Non-persisted collapse still works for this session.
      }
      return next;
    });
  }, [mobile, mobileSidebarOpen, closeMobileSidebar]);

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia('(max-width: 767px)');
    const sync = () => {
      const hadSidebarFocus = document
        .getElementById('agents-sidebar')
        ?.contains(document.activeElement);
      setMobile(query.matches);
      setMobileSidebarOpen(false);
      if (hadSidebarFocus)
        window.requestAnimationFrame(() => document.getElementById('agents-workspace')?.focus());
    };
    sync();
    query.addEventListener('change', sync);
    return () => query.removeEventListener('change', sync);
  }, []);

  useEffect(() => setMobileSidebarOpen(false), [pathname]);

  useEffect(() => {
    if (!mobileSidebarOpen) return;
    document
      .getElementById('agents-sidebar')
      ?.querySelector<HTMLElement>('a[href], button:not([disabled]), input:not([disabled])')
      ?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.defaultPrevented) return;
      setMobileSidebarOpen(false);
      document.getElementById('agents-sidebar-toggle')?.focus();
    };
    window.addEventListener('keydown', closeOnEscape);
    return () => window.removeEventListener('keydown', closeOnEscape);
  }, [mobileSidebarOpen]);

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
      <McpApprovalsProvider
        ownerId={state.user?.id}
        enabled={hasAgentPermission(state.user?.agent_access, 'agent.use')}
      >
        <div className="fixed inset-0 z-50 flex flex-col bg-gray-50 dark:bg-gray-950">
          <AgentsHeader
            sidebarToggle={
              mobile || sidebarCollapsed ? (
                <SidebarToggleButton
                  sidebarCollapsed={mobile ? !mobileSidebarOpen : sidebarCollapsed}
                  onToggleSidebar={toggleSidebar}
                />
              ) : undefined
            }
          />
          <AgentsShell
            sidebarCollapsed={mobile ? !mobileSidebarOpen : sidebarCollapsed}
            sidebarToggle={
              !mobile && !sidebarCollapsed ? (
                <SidebarToggleButton sidebarCollapsed={false} onToggleSidebar={toggleSidebar} />
              ) : undefined
            }
            mobileSidebarOpen={mobileSidebarOpen}
            onSidebarNavigate={closeMobileSidebar}
          >
            {children}
          </AgentsShell>
          <DeploymentFooter />
        </div>
      </McpApprovalsProvider>
    </ProtectedRoute>
  );
}
