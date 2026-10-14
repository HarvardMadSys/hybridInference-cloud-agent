// The site configuration parser, exercised against a literal environment.
//
// These are the cases that decide whether a deployment can be misconfigured
// silently. Two properties matter more than the happy path:
//
//   * an illegal value is refused rather than read as "unset", because the old
//     reader turned a typo into a missing link and said nothing; and
//   * the output is an allowlist, so no environment variable outside the
//     named ones can reach the page.

import { describe, expect, it } from 'vitest';

import { NEUTRAL_SITE_CONFIG, SiteConfigError, parseRuntimeSiteConfig } from './site';

const FULL_ENV = {
  AGENT_WEB_APP_NAME: 'Acme Agents',
  AGENT_WEB_ORG_NAME: 'Acme Research',
  AGENT_WEB_ORG_URL: 'https://acme.example/about',
  AGENT_WEB_STATUS_URL: 'https://status.acme.example/',
  AGENT_WEB_DOCS_URL: 'https://docs.acme.example/agents/',
  AGENT_WEB_CONTACT_URL: 'MAILTO:support@acme.example',
  AGENT_WEB_GATEWAY_URL: 'https://gateway.acme.example/authorize',
  AGENT_WEB_FEATURE_RAG: 'true',
};

describe('parseRuntimeSiteConfig', () => {
  it('falls back to the neutral identity when nothing is configured', () => {
    expect(parseRuntimeSiteConfig({})).toEqual(NEUTRAL_SITE_CONFIG);
  });

  it('reads the eight documented variables and nothing else', () => {
    expect(parseRuntimeSiteConfig(FULL_ENV)).toEqual({
      branding: {
        appName: 'Acme Agents',
        orgName: 'Acme Research',
        orgUrl: 'https://acme.example/about',
        statusUrl: 'https://status.acme.example/',
        docsUrl: 'https://docs.acme.example/agents/',
        contactUrl: 'mailto:support@acme.example',
        // Reduced to the origin: the links built from it are `/chat` and
        // `/dashboard`, so a path in the configured URL is not carried.
        gatewayUrl: 'https://gateway.acme.example',
      },
      features: { rag: true },
    });
  });

  it('treats a blank name as unset rather than rendering an empty header', () => {
    expect(parseRuntimeSiteConfig({ AGENT_WEB_APP_NAME: '   ' }).branding.appName).toBe(
      NEUTRAL_SITE_CONFIG.branding.appName,
    );
  });

  it('keeps optional links hidden when they are unset or empty', () => {
    const site = parseRuntimeSiteConfig({ AGENT_WEB_ORG_URL: '', AGENT_WEB_STATUS_URL: '  ' });
    expect(site.branding.orgUrl).toBe('');
    expect(site.branding.statusUrl).toBe('');
    expect(site.branding.docsUrl).toBe('');
    expect(site.branding.contactUrl).toBe('');
    expect(site.branding.gatewayUrl).toBe('');
  });

  it.each([
    ['AGENT_WEB_ORG_URL', 'not a url'],
    ['AGENT_WEB_STATUS_URL', 'status.example.test'],
    ['AGENT_WEB_DOCS_URL', 'docs.example.test'],
    ['AGENT_WEB_CONTACT_URL', 'support@example.test'],
    ['AGENT_WEB_CONTACT_URL', 'mailto:support@example.test?subject=Hi'],
    ['AGENT_WEB_GATEWAY_URL', '://missing-scheme'],
  ])('refuses an unparseable %s instead of hiding the link', (name, value) => {
    expect(() => parseRuntimeSiteConfig({ [name]: value })).toThrow(SiteConfigError);
    expect(() => parseRuntimeSiteConfig({ [name]: value })).toThrow(name);
  });

  it.each([
    ['AGENT_WEB_ORG_URL', 'javascript:alert(1)'],
    ['AGENT_WEB_STATUS_URL', 'ftp://status.example.test/'],
    ['AGENT_WEB_DOCS_URL', 'javascript:alert(1)'],
    ['AGENT_WEB_CONTACT_URL', 'javascript:alert(1)'],
    ['AGENT_WEB_GATEWAY_URL', 'data:text/html,hi'],
  ])('refuses a non-http(s) %s', (name, value) => {
    expect(() => parseRuntimeSiteConfig({ [name]: value })).toThrow(/must be an http\(s\) URL/);
  });

  it.each([
    ['true', true],
    ['TRUE', true],
    [' false ', false],
    ['', false],
    [undefined, false],
  ])('parses the rag switch %j as %j', (raw, expected) => {
    const env = raw === undefined ? {} : { AGENT_WEB_FEATURE_RAG: raw };
    expect(parseRuntimeSiteConfig(env).features.rag).toBe(expected);
  });

  it.each(['1', 'yes', 'on', 'truthy'])('refuses the ambiguous rag value %j', (raw) => {
    expect(() => parseRuntimeSiteConfig({ AGENT_WEB_FEATURE_RAG: raw })).toThrow(
      /must be "true" or "false"/,
    );
  });

  it('exposes exactly the declared fields, whatever else is in the environment', () => {
    const sentinel = 'sentinel-do-not-publish';
    const site = parseRuntimeSiteConfig({
      ...FULL_ENV,
      AGENT_SESSION_SECRET: sentinel,
      AGENT_WEB_SOMETHING_NEW: sentinel,
      AGENT_WEB_APP_NAME_EXTRA: sentinel,
    });

    expect(Object.keys(site).sort()).toEqual(['branding', 'features']);
    expect(Object.keys(site.branding).sort()).toEqual([
      'appName',
      'contactUrl',
      'docsUrl',
      'gatewayUrl',
      'orgName',
      'orgUrl',
      'statusUrl',
    ]);
    expect(Object.keys(site.features)).toEqual(['rag']);
    expect(JSON.stringify(site)).not.toContain(sentinel);
  });

  it('normalizes the deployment timestamp to UTC and reads the release commit', () => {
    expect(
      parseRuntimeSiteConfig({
        AGENT_DEPLOYMENT_SHA: ' abc1234 ',
        AGENT_WEB_DEPLOYED_AT: '2026-08-07T23:30:00-04:00',
      }).deployment,
    ).toEqual({ version: 'abc1234', deployedAt: '2026-08-08T03:30:00.000Z' });
  });

  it.each(['not a date', '2026-08-07', '2026-08-07T12:00:00'])(
    'rejects an invalid or ambiguous deployment date %s',
    (date) => {
      expect(() => parseRuntimeSiteConfig({ AGENT_WEB_DEPLOYED_AT: date })).toThrow(
        'AGENT_WEB_DEPLOYED_AT',
      );
    },
  );
});
