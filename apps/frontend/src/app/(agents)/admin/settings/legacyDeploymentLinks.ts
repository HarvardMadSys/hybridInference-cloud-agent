/**
 * Links into the removed Deployment setup section.
 *
 * Older pages linked to `#application-bootstrap-<key>` and a preview build to
 * `#deployment-<id>`. A row that an editable setting manages opens that
 * setting; every other row has no editor here, so the link opens the settings
 * page normally rather than pointing at a section that no longer exists.
 */
const LEGACY_PREFIXES = ['#deployment-', '#application-bootstrap-'] as const;

/** Former row ID or response key → the application setting that manages it. */
const EDITABLE_SETTING: Record<string, string> = {
  github_app_key_file: 'github_app_private_key',
  AGENT_GITHUB_APP_PRIVATE_KEY_PATH: 'github_app_private_key',
  pgadmin_bootstrap: 'pgadmin_url',
  'AGENT_PGADMIN_BOOTSTRAP_URL / AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL / AGENT_PGADMIN_BOOTSTRAP_DIR':
    'pgadmin_url',
};

export function isLegacyDeploymentHash(hash: string): boolean {
  return LEGACY_PREFIXES.some((prefix) => hash.startsWith(prefix));
}

/** The setting a former Deployment link should open, or null when none manages it. */
export function legacyDeploymentSetting(hash: string): string | null {
  const prefix = LEGACY_PREFIXES.find((candidate) => hash.startsWith(candidate));
  if (!prefix) return null;
  const fragment = hash.slice(prefix.length);
  let decoded = fragment;
  try {
    decoded = decodeURIComponent(fragment);
  } catch {
    // A malformed escape can still match a raw fragment.
  }
  for (const candidate of [fragment, decoded]) {
    if (Object.prototype.hasOwnProperty.call(EDITABLE_SETTING, candidate)) {
      return EDITABLE_SETTING[candidate];
    }
  }
  return null;
}
