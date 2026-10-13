// Deployment site identity, resolved from the environment at request time.
//
// The build-time half of the configuration stays in `env.ts`: `basePath` and
// the browser's API prefix are properties of the *image variant*, and Next
// resolves them while the bundle is compiled. Everything here is the opposite
// — a name, an organisation, a status page, the gateway's origin — and those
// are properties of a *deployment*, not of a build. A deployment that changes
// its product name must not have to rebuild the same image.
//
// **This module is pure.** It reads no `process.env` of its own and imports
// nothing from Next, so it can be unit-tested against a literal environment.
// The server-only reader that supplies the real environment lives in
// `site.server.ts`; nothing under a client component may import that one.
//
// The output is an explicit allowlist. There is deliberately no spread of
// `process.env` and no generic "read any `AGENT_WEB_*`" helper: a field that
// is not named below cannot reach the browser, which is what keeps a secret
// that happens to share the prefix from being published by accident.

/** The identity the shell renders. Mirrors the old `RuntimeSiteConfig`. */
export interface SiteBranding {
  appName: string;
  orgName: string;
  orgUrl: string;
  statusUrl: string;
  /**
   * The gateway's *origin*, for the links this app does not host itself.
   * Never the browser's API address — that is `basePath` plus `/api`, and it
   * belongs to the build variant.
   */
  gatewayUrl: string;
}

export interface RuntimeSiteConfig {
  branding: SiteBranding;
  deployment?: {
    version: string;
    deployedAt: string;
  };
  features: {
    /** The gateway's docs assistant. Off unless a deployment says otherwise. */
    rag: boolean;
  };
}

/**
 * What a deployment that configured nothing renders.
 *
 * Deliberately generic. It is also the context's default value, so a component
 * rendered outside the provider shows *this* rather than a previous
 * deployment's brand — a fallback that named a real site is how the wrong
 * product name reaches a page nobody thought to check.
 */
export const NEUTRAL_SITE_CONFIG: RuntimeSiteConfig = {
  branding: {
    appName: 'Cloud Agent',
    orgName: '',
    orgUrl: '',
    statusUrl: '',
    gatewayUrl: '',
  },
  features: {
    rag: false,
  },
};

/**
 * The environment variables this module reads, named once.
 *
 * Quoted string literals rather than property access on `process.env`, so the
 * inventory test in the backend suite can see them and demand a row in
 * ENVIRONMENT.md. A variable read through `process.env[name]` is invisible to
 * that check, and an undocumented operator knob is one nobody turns.
 */
export const SITE_CONFIG_ENV = {
  appName: 'AGENT_WEB_APP_NAME',
  orgName: 'AGENT_WEB_ORG_NAME',
  orgUrl: 'AGENT_WEB_ORG_URL',
  statusUrl: 'AGENT_WEB_STATUS_URL',
  gatewayUrl: 'AGENT_WEB_GATEWAY_URL',
  featureRag: 'AGENT_WEB_FEATURE_RAG',
  version: 'AGENT_DEPLOYMENT_SHA',
  deployedAt: 'AGENT_WEB_DEPLOYED_AT',
} as const;

type EnvSource = Record<string, string | undefined>;

/**
 * A deployment whose configuration cannot be rendered.
 *
 * Thrown rather than swallowed. The previous code turned an unparseable URL
 * into the empty string, which reads as "not configured" — so a typo in a
 * status URL silently removed the Status link, and a typo in the gateway URL
 * silently removed every gateway link, with nothing anywhere saying why. A
 * deployment that meant to configure something and got it wrong should say so.
 */
export class SiteConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SiteConfigError';
  }
}

function trimmed(value: string | undefined): string {
  return (value ?? '').trim();
}

/** An optional name. Empty and unset are the same answer: hide it. */
function optionalName(env: EnvSource, name: string): string {
  return trimmed(env[name]);
}

/** An optional absolute http(s) URL, kept whole (path included). */
function optionalUrl(env: EnvSource, name: string): string {
  const raw = trimmed(env[name]);
  if (!raw) return '';
  return parseUrl(name, raw).toString();
}

/** An optional absolute http(s) URL, reduced to its origin. */
function optionalOrigin(env: EnvSource, name: string): string {
  const raw = trimmed(env[name]);
  if (!raw) return '';
  return parseUrl(name, raw).origin;
}

function parseUrl(name: string, raw: string): URL {
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    throw new SiteConfigError(`${name} is not a valid absolute URL: ${raw}`);
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new SiteConfigError(`${name} must be an http(s) URL, got: ${raw}`);
  }
  return parsed;
}

/**
 * An optional strict boolean.
 *
 * `true` and `false` only, case-insensitive; empty and unset fall back. The
 * old reader was `=== 'true'`, which silently read `1`, `yes` and `TRUE` as
 * off — a switch an operator sets and cannot see that it did nothing.
 */
function optionalBoolean(env: EnvSource, name: string, fallback: boolean): boolean {
  const raw = trimmed(env[name]);
  if (!raw) return fallback;
  const lowered = raw.toLowerCase();
  if (lowered === 'true') return true;
  if (lowered === 'false') return false;
  throw new SiteConfigError(`${name} must be "true" or "false", got: ${raw}`);
}

/**
 * Resolve the site identity from an explicit environment.
 *
 * Takes the environment as an argument rather than reaching for the global
 * one, so the tests exercise the real parser against a literal map instead of
 * mutating `process.env` and hoping the module re-reads it.
 */
export function parseRuntimeSiteConfig(env: EnvSource): RuntimeSiteConfig {
  const site: RuntimeSiteConfig = {
    branding: {
      appName: optionalName(env, SITE_CONFIG_ENV.appName) || NEUTRAL_SITE_CONFIG.branding.appName,
      orgName: optionalName(env, SITE_CONFIG_ENV.orgName),
      orgUrl: optionalUrl(env, SITE_CONFIG_ENV.orgUrl),
      statusUrl: optionalUrl(env, SITE_CONFIG_ENV.statusUrl),
      gatewayUrl: optionalOrigin(env, SITE_CONFIG_ENV.gatewayUrl),
    },
    features: {
      rag: optionalBoolean(env, SITE_CONFIG_ENV.featureRag, NEUTRAL_SITE_CONFIG.features.rag),
    },
  };
  const version = optionalName(env, SITE_CONFIG_ENV.version);
  const rawDate = optionalName(env, SITE_CONFIG_ENV.deployedAt);
  let deployedAt = '';
  if (rawDate) {
    const date = new Date(rawDate);
    if (
      !/^\d{4}-\d{2}-\d{2}T.+(?:Z|[+-]\d{2}:\d{2})$/.test(rawDate) ||
      !Number.isFinite(date.getTime())
    ) {
      throw new SiteConfigError(
        `${SITE_CONFIG_ENV.deployedAt} must be an ISO timestamp with a timezone`,
      );
    }
    deployedAt = date.toISOString();
  }
  if (version || deployedAt) site.deployment = { version, deployedAt };
  return site;
}

function checkedSavedSiteConfig(value: unknown, metadata: EnvSource): RuntimeSiteConfig {
  if (!value || typeof value !== 'object')
    throw new SiteConfigError('Invalid application settings.');
  const saved = value as Record<string, unknown>;
  const branding = saved.branding as Record<string, unknown> | undefined;
  const features = saved.features as Record<string, unknown> | undefined;
  if (!branding || !features || typeof features.rag !== 'boolean') {
    throw new SiteConfigError('Invalid application settings.');
  }
  const env: EnvSource = {
    [SITE_CONFIG_ENV.version]: metadata[SITE_CONFIG_ENV.version],
    [SITE_CONFIG_ENV.deployedAt]: metadata[SITE_CONFIG_ENV.deployedAt],
    [SITE_CONFIG_ENV.featureRag]: String(features.rag),
  };
  for (const key of ['appName', 'orgName', 'orgUrl', 'statusUrl', 'gatewayUrl'] as const) {
    if (typeof branding[key] !== 'string')
      throw new SiteConfigError('Invalid application settings.');
    env[SITE_CONFIG_ENV[key]] = branding[key];
  }
  return parseRuntimeSiteConfig(env);
}

/** Only validated public appearance can become the server's outage fallback. */
export function parseSavedPublicAppearance(
  value: unknown,
): Pick<RuntimeSiteConfig, 'branding' | 'features'> | null {
  try {
    const { branding, features } = checkedSavedSiteConfig(value, {});
    return { branding, features };
  } catch {
    return null;
  }
}

/** Keep the application accessible if saved appearance settings cannot be rendered. */
export function parseSavedSiteConfig(value: unknown, metadata: EnvSource = {}): RuntimeSiteConfig {
  try {
    return checkedSavedSiteConfig(value, metadata);
  } catch {
    // Never restore old environment branding or carry through unknown response
    // fields. Only the server-owned deployment identity accompanies defaults.
    return parseRuntimeSiteConfig({
      [SITE_CONFIG_ENV.version]: metadata[SITE_CONFIG_ENV.version],
      [SITE_CONFIG_ENV.deployedAt]: metadata[SITE_CONFIG_ENV.deployedAt],
    });
  }
}
