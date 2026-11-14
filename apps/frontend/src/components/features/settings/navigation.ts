export const SETTINGS_PATHS = {
  profile: '/settings/my-settings',
  account: '/settings/account',
  models: '/settings/models',
  memory: '/settings/memory',
  shortcuts: '/settings/keyboard-shortcuts',
  usage: '/settings/usage',
  plugins: '/settings/plugins',
  integrations: '/settings/integrations',
  ssh: '/settings/ssh-connections',
  environments: '/settings/environments',
  teams: '/settings/teams',
  help: '/settings/getting-started',
} as const;

/** Model connections lived in My Settings under this anchor before they had a page. */
export const MODEL_CONNECTIONS_ANCHOR = '#settings-connections';

/**
 * Where a link that means "set up your own connection" goes. The anchor opens
 * the section, which is folded where the deployment provides models.
 */
export const MODEL_CONNECTIONS_HREF = `${SETTINGS_PATHS.models}${MODEL_CONNECTIONS_ANCHOR}`;

/**
 * What such a link says to a Models page that is already drawn. Followed
 * there, it changes the fragment and nothing else: the page does not remount,
 * and the router's `pushState` fires no `hashchange`, so the address alone
 * would leave a folded section folded.
 */
export const MODEL_CONNECTIONS_REQUESTED = 'settings.model-connections.requested';

export function requestModelConnections() {
  window.dispatchEvent(new Event(MODEL_CONNECTIONS_REQUESTED));
}

export function isSettingsPath(pathname: string): boolean {
  return (
    pathname === '/settings' || pathname.startsWith('/settings/') || pathname === '/integrations'
  );
}

/** Old bookmarks and provider callbacks retain their query and section anchor. */
export function legacySettingsDestination(search: string, hash: string): string {
  const params = new URLSearchParams(search);
  let path: string = SETTINGS_PATHS.profile;
  if (['mcp_authorized', 'mcp_connection', 'mcp_message'].some((key) => params.has(key))) {
    path = SETTINGS_PATHS.plugins;
  } else if (params.has('connected')) {
    path = SETTINGS_PATHS.integrations;
  } else if (/mcp|skills/.test(hash)) {
    path = SETTINGS_PATHS.plugins;
  } else if (hash === '#source-control-heading') {
    path = SETTINGS_PATHS.integrations;
  } else if (hash === '#settings-user-memory' || hash === '#settings-memory') {
    path = SETTINGS_PATHS.memory;
  } else if (hash === '#settings-shortcuts') {
    path = SETTINGS_PATHS.shortcuts;
  } else if (hash === MODEL_CONNECTIONS_ANCHOR) {
    path = SETTINGS_PATHS.models;
  } else if (hash === '#settings-environments') {
    path = SETTINGS_PATHS.environments;
  } else if (hash === '#settings-getting-started') {
    path = SETTINGS_PATHS.help;
  }
  const query = params.toString();
  return `${path}${query ? `?${query}` : ''}${hash}`;
}

/** Only chat routes may become the return destination, never an auth/admin page. */
export function isChatPath(pathname: string): boolean {
  return (
    pathname === '/' ||
    /^\/(?!settings$|integrations$|connected$|archived$|approvals$|admin$|login$|setup$)[a-zA-Z0-9_-]+$/.test(
      pathname,
    )
  );
}
