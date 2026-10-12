'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { GettingStartedSettings } from '@/components/features/settings/GettingStartedSettings';
import { NotificationSettings } from '@/components/features/settings/NotificationSettings';
import { UserMemorySettings } from '@/components/features/settings/UserMemorySettings';
import { ProjectMemorySettings } from '@/components/features/settings/ProjectMemorySettings';
import {
  disconnectAgentIntegration,
  disconnectGitHubAccount,
  getAgentIntegrations,
} from '@/lib/api/agents';
import type { AgentIntegrationProvider, AgentIntegrationProviderApi } from '@/lib/api/agents';
import { SkillsSection } from './skills/SkillsSection';
import { McpConnectionsSection, type McpAuthorizationResult } from './mcp/McpConnectionsSection';
import { SOURCE_CONTROL_RECHECK_MS } from './sourceControlRecovery';

const PROVIDERS: AgentIntegrationProvider[] = ['github', 'gitlab'];

/** What `disconnecting` holds while the whole GitHub connection is going. */
const GITHUB_ACCOUNT = 'github-account';

/** Providers shown, but not yet offered to connect.
 *
 * GitLab's OAuth half works and would list projects — but agent jobs and merge
 * requests do not, so connecting it buys a user nothing they came here for and
 * costs them a token grant to a source-control account. "Coming soon" is the
 * honest state; the alternative was a Connect button whose result was a
 * sentence explaining what it could not do.
 */
const COMING_SOON: AgentIntegrationProvider[] = ['gitlab'];

const PROVIDER_COPY: Record<AgentIntegrationProvider, { name: string; description: string }> = {
  github: {
    name: 'GitHub',
    description: 'Connect GitHub for agent jobs and enhanced codebase context',
  },
  gitlab: {
    name: 'GitLab',
    description: 'Agent jobs and merge requests on GitLab are not supported yet.',
  },
};

function emptyProvider(provider: AgentIntegrationProvider): AgentIntegrationProviderApi {
  return {
    provider,
    configured: false,
    connected: false,
    connect_url: null,
    capabilities: [],
    accounts: [],
    repositories: [],
  };
}

function ProviderIcon({ provider }: { provider: AgentIntegrationProvider }) {
  return (
    <span className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl border border-border bg-surface-muted text-foreground">
      {provider === 'github' ? (
        <svg className="h-5 w-5" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
          <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
        </svg>
      ) : (
        <svg className="h-5 w-5" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <path d="m12 21.5-4.42-13.6h8.84L12 21.5Zm0 0L3.4 15.25l4.18-7.35L12 21.5Zm0 0 8.6-6.25-4.18-7.35L12 21.5ZM3.4 15.25l-1.1-3.38a.76.76 0 0 1 .03-.55L5.27 3.5a.38.38 0 0 1 .72.04L7.58 7.9 3.4 15.25Zm17.2 0 1.1-3.38a.76.76 0 0 0-.03-.55L18.73 3.5a.38.38 0 0 0-.72.04L16.42 7.9l4.18 7.35Z" />
        </svg>
      )}
    </span>
  );
}

function connectedDescription(provider: AgentIntegrationProviderApi): React.ReactNode {
  const accountLabels = provider.accounts.map((account) => account.label).filter(Boolean);
  const repositoryCount = provider.repositories.length;

  // The connection is intact; the deployment cannot use it right now. Not
  // "Connected" with no repositories, which reads as access that was lost.
  if (provider.load_status === 'unavailable') {
    return (
      <>
        {accountLabels.length > 0 ? `${accountLabels.join(', ')} · ` : ''}
        Temporarily unavailable
      </>
    );
  }

  return (
    <>
      Connected
      {accountLabels.length > 0 ? ` as ${accountLabels.join(', ')}` : ''}
      {repositoryCount > 0
        ? ` · ${repositoryCount} ${repositoryCount === 1 ? 'repository' : 'repositories'}`
        : ''}
      {provider.provider === 'gitlab'
        ? ' · Project discovery only; Agent jobs and merge requests are not supported yet.'
        : ''}
    </>
  );
}

function ProviderActions({
  integration,
  disconnecting,
  onDisconnect,
  onDisconnectAll,
}: {
  integration: AgentIntegrationProviderApi;
  disconnecting: string | null;
  onDisconnect: (connectionId: string, label: string) => void;
  onDisconnectAll: () => void;
}) {
  const copy = PROVIDER_COPY[integration.provider];

  if (!integration.connected) {
    // Before the `configured` check: whether an operator has set the OAuth app
    // up is not something a user can act on while the feature is not offered,
    // and "Admin setup required" reads as "ask someone and you can have it".
    if (COMING_SOON.includes(integration.provider)) {
      return (
        <span className="rounded-full border border-border bg-surface-muted px-3 py-1.5 text-xs font-medium text-muted">
          Coming soon
        </span>
      );
    }
    // Before the `configured` check, which is also false while the App is
    // unavailable: that one means nobody set the provider up, and sends an
    // operator to check configuration that is already correct.
    if (integration.load_status === 'unavailable') {
      return (
        <span className="text-sm text-gray-400 dark:text-gray-500">Temporarily unavailable</span>
      );
    }
    if (!integration.configured) {
      return <span className="text-sm text-gray-500 dark:text-gray-400">Admin setup required</span>;
    }
    return integration.connect_url ? (
      <a href={integration.connect_url} className="ui-button ui-button-primary min-h-11">
        Connect
        <svg className="h-4 w-4" viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <path
            d="M7 13 13 7m0 0H8m5 0v5"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </a>
    ) : (
      <span className="text-sm text-gray-400 dark:text-gray-500">Temporarily unavailable</span>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      {/* Out of the menu. It is the one way to add repositories — installing
          the App on a personal account or another organization — and a member
          of an organization that already had the App never saw it: connecting
          found that installation, so GitHub's install page was skipped. */}
      {integration.manage_url ? (
        <a href={integration.manage_url} className="ui-button ui-button-secondary min-h-11">
          Configure repositories
        </a>
      ) : null}
      <details className="group relative w-full lg:w-auto">
        <summary className="ui-button ui-button-secondary min-h-11 cursor-pointer list-none [&::-webkit-details-marker]:hidden">
          Manage
          <svg
            className="h-4 w-4 transition-transform group-open:rotate-180"
            viewBox="0 0 20 20"
            fill="none"
            aria-hidden="true"
          >
            <path
              d="m7 8 3 3 3-3"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </summary>
        <div className="ui-card absolute left-0 z-10 mt-2 w-56 max-w-[calc(100vw-3rem)] bg-surface p-1.5 shadow-lg lg:left-auto lg:right-0">
          {/* GitHub has one way out and nothing else here. Reauthorizing only
              mattered for an installation made outside this page, which
              disconnecting and connecting again also picks up; removing one
              installation lasted only until the next connection, which makes
              the grants whatever GitHub reports. Which repositories the App
              may use is changed on GitHub, through Configure repositories. */}
          {integration.provider !== 'github' && integration.connect_url ? (
            <a
              href={integration.connect_url}
              className="ui-focus-ring flex min-h-11 items-center rounded-lg px-3 py-2 text-sm text-foreground hover:bg-surface-hover"
            >
              Reauthorize {copy.name}
            </a>
          ) : null}
          {(integration.provider === 'github' ? [] : integration.accounts).map((account) => (
            <button
              key={account.id}
              type="button"
              disabled={disconnecting === account.id}
              onClick={() => onDisconnect(account.id, account.label)}
              className="ui-focus-ring block min-h-11 w-full rounded-lg px-3 py-2 text-left text-sm text-status-error hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-60 dark:hover:bg-red-500/10"
            >
              {disconnecting === account.id
                ? `Disconnecting ${account.label}…`
                : `Disconnect ${account.label}`}
            </button>
          ))}
          {integration.provider === 'github' ? (
            <button
              type="button"
              disabled={disconnecting === GITHUB_ACCOUNT}
              onClick={onDisconnectAll}
              className="ui-focus-ring block min-h-11 w-full rounded-lg px-3 py-2 text-left text-sm text-status-error hover:bg-red-50 disabled:cursor-not-allowed disabled:opacity-60 dark:hover:bg-red-500/10"
            >
              {disconnecting === GITHUB_ACCOUNT ? 'Disconnecting GitHub…' : 'Disconnect GitHub'}
            </button>
          ) : null}
        </div>
      </details>
    </div>
  );
}

export function IntegrationsView({
  connectedProvider,
  mcpAuthorization,
}: {
  connectedProvider?: AgentIntegrationProvider | null;
  mcpAuthorization?: McpAuthorizationResult;
}) {
  const t = useT();
  const [providers, setProviders] = useState<AgentIntegrationProviderApi[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [disconnecting, setDisconnecting] = useState<string | null>(null);
  const connectedNoticeChecked = useRef(false);
  // Which request the page is waiting for. Only the most recently *sent*
  // answer may land: a re-check sent before a disconnect can arrive after the
  // reload that followed it, and would put the removed account back.
  const latestLoad = useRef(0);

  // `quiet` is the background re-check below: it replaces the rows when the
  // answer arrives and otherwise leaves the page as it is — no skeleton, and
  // no error banner for a check the user did not ask for.
  const load = useCallback(
    async ({ quiet = false }: { quiet?: boolean } = {}) => {
      const request = ++latestLoad.current;
      if (!quiet) {
        setLoading(true);
        setError(null);
      }
      try {
        const response = await getAgentIntegrations();
        if (request !== latestLoad.current) return;
        setProviders(response.providers);
        if (!connectedNoticeChecked.current && connectedProvider) {
          connectedNoticeChecked.current = true;
          const verified = response.providers.find(
            (provider) => provider.provider === connectedProvider && provider.connected,
          );
          if (verified) setNotice(`${PROVIDER_COPY[connectedProvider].name} connected.`);
        }
      } catch (cause: unknown) {
        if (!quiet && request === latestLoad.current) {
          setError(cause instanceof Error ? cause.message : 'Could not load integrations.');
        }
      } finally {
        // The latest request ends the loading state, quiet or not: a load it
        // superseded must not clear the skeleton early, nor leave it on.
        if (request === latestLoad.current) setLoading(false);
      }
    },
    [connectedProvider],
  );

  useEffect(() => {
    void load();
  }, [load]);

  // The service reloads a GitHub App that failed to load, so a page showing
  // "Temporarily unavailable" looks again until it is back. GitLab is not
  // retried by the service, so looking again could not change its answer.
  const githubUnavailable = providers.some(
    (provider) => provider.provider === 'github' && provider.load_status === 'unavailable',
  );
  useEffect(() => {
    if (!githubUnavailable) return undefined;
    const timer = window.setInterval(() => void load({ quiet: true }), SOURCE_CONTROL_RECHECK_MS);
    return () => window.clearInterval(timer);
  }, [githubUnavailable, load]);

  async function disconnectGitHub() {
    setDisconnecting(GITHUB_ACCOUNT);
    setError(null);
    setNotice(null);
    // The answer is the page's whole state; a check already on its way must
    // not land on top of it.
    const request = ++latestLoad.current;
    try {
      const response = await disconnectGitHubAccount();
      if (request === latestLoad.current) setProviders(response.providers);
      setNotice('GitHub disconnected.');
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not disconnect GitHub.');
    } finally {
      setDisconnecting(null);
    }
  }

  async function disconnect(
    provider: AgentIntegrationProvider,
    connectionId: string,
    label: string,
  ) {
    setDisconnecting(connectionId);
    setError(null);
    setNotice(null);
    try {
      await disconnectAgentIntegration(provider, connectionId);
      setNotice(`${label} disconnected.`);
      await load();
    } catch (cause: unknown) {
      setError(cause instanceof Error ? cause.message : 'Could not disconnect the integration.');
    } finally {
      setDisconnecting(null);
    }
  }

  const providersByName = new Map(providers.map((provider) => [provider.provider, provider]));

  return (
    <section className="mx-auto w-full max-w-5xl px-4 py-8 sm:px-6 lg:px-10 lg:py-10">
      <header className="border-b border-border pb-6">
        <h1 className="text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">
          {t('settings.title')}
        </h1>
        <p className="mt-2 max-w-xl text-sm leading-6 text-muted sm:text-base">
          {t('settings.description')}
        </p>
      </header>

      {notice ? (
        <div
          className="mt-6 rounded-xl border border-emerald-200 dark:border-emerald-500/25 bg-emerald-50 dark:bg-emerald-500/10 px-4 py-3 text-sm text-emerald-800 dark:text-emerald-300"
          role="status"
        >
          {notice}
        </div>
      ) : null}
      {error ? (
        <div
          className="mt-6 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-red-200 dark:border-red-500/25 bg-red-50 dark:bg-red-500/10 px-4 py-3 text-sm text-red-700 dark:text-red-400"
          role="alert"
        >
          <span className="min-w-0 break-words">{error}</span>
          <button
            type="button"
            onClick={() => void load()}
            className="ui-button ui-focus-ring min-h-11 shrink-0 hover:bg-red-100 dark:hover:bg-red-500/10"
          >
            Try again
          </button>
        </div>
      ) : null}

      <section
        aria-labelledby="source-control-heading"
        className="ui-card mt-6 rounded-2xl bg-surface px-4 sm:px-6"
      >
        <h2
          id="source-control-heading"
          className="border-b border-border py-5 text-base font-semibold text-foreground"
        >
          Source Control
        </h2>
        <div>
          {PROVIDERS.map((provider, index) => {
            const foundIntegration = providersByName.get(provider);
            const integration = foundIntegration ?? emptyProvider(provider);
            const statusUnavailable = Boolean(error && !foundIntegration);
            const copy = PROVIDER_COPY[provider];
            return (
              <div
                key={provider}
                className={`flex min-h-24 flex-col justify-center gap-4 py-5 lg:flex-row lg:items-center ${
                  index > 0 ? 'border-t border-border' : ''
                }`}
              >
                <div className="flex min-w-0 flex-1 items-start gap-3 sm:gap-4">
                  <ProviderIcon provider={provider} />
                  <div className="min-w-0">
                    <h3 className="text-sm font-semibold text-foreground">{copy.name}</h3>
                    <p className="mt-1 break-words text-sm leading-6 text-muted">
                      {loading
                        ? 'Loading connection status…'
                        : statusUnavailable
                          ? 'Connection status is unavailable'
                          : integration.error
                            ? integration.error
                            : integration.connected
                              ? connectedDescription(integration)
                              : copy.description}
                    </p>
                  </div>
                </div>
                <div className="flex min-w-0 shrink-0 flex-wrap items-center gap-2 lg:justify-end">
                  {loading ? (
                    <span
                      className="h-11 w-24 animate-pulse rounded-lg bg-surface-muted"
                      aria-label={`Loading ${copy.name}`}
                    />
                  ) : statusUnavailable ? (
                    <span className="text-sm text-gray-400 dark:text-gray-500">Unavailable</span>
                  ) : (
                    <ProviderActions
                      integration={integration}
                      disconnecting={disconnecting}
                      onDisconnect={(connectionId, label) =>
                        void disconnect(provider, connectionId, label)
                      }
                      onDisconnectAll={() => void disconnectGitHub()}
                    />
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </section>

      <div className="ui-card mt-6 rounded-2xl bg-surface p-4 sm:p-6">
        <McpConnectionsSection authorizationResult={mcpAuthorization} />
      </div>
      <SkillsSection />
      <UserMemorySettings />
      <ProjectMemorySettings />
      <NotificationSettings />
      <GettingStartedSettings />
    </section>
  );
}
