export const SETTINGS_PATHS = {
  profile: '/settings/my-settings',
  usage: '/settings/usage',
  plugins: '/settings/plugins',
  integrations: '/settings/integrations',
  environments: '/settings/environments',
  help: '/settings/getting-started',
} as const;

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
