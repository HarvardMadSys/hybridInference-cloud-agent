import { describe, expect, it } from 'vitest';

import { NEUTRAL_SITE_CONFIG, parseSavedSiteConfig, parseSavedPublicAppearance } from './site';

const saved = {
  branding: {
    appName: 'Admin configured app',
    orgName: 'Organization',
    orgUrl: 'https://example.com/about',
    statusUrl: '',
    docsUrl: 'https://docs.example.com/agents/',
    contactUrl: 'https://example.com/support',
    gatewayUrl: 'https://gateway.example.com/dashboard',
  },
  features: { rag: true },
};

describe('saved public site settings', () => {
  it('reads a control plane that predates the documentation link as having none', () => {
    const { docsUrl: _omitted, contactUrl: _alsoOmitted, ...olderBranding } = saved.branding;
    const site = parseSavedSiteConfig({ ...saved, branding: olderBranding });
    expect(site.branding.appName).toBe('Admin configured app');
    expect(site.branding.docsUrl).toBe('');
    expect(site.branding.contactUrl).toBe('');
  });

  it('reads a contact link that is a page or one mail address', () => {
    for (const contactUrl of ['https://example.com/support', 'mailto:help@example.com']) {
      const site = parseSavedSiteConfig({ ...saved, branding: { ...saved.branding, contactUrl } });
      expect(site.branding.contactUrl).toBe(contactUrl);
    }
  });

  it('rejects a contact link that could prefill a message or run script', () => {
    for (const contactUrl of [
      'javascript:alert(1)',
      'help@example.com',
      'mailto:help@example.com?subject=Hi',
      'mailto:a@example.com,b@example.com',
      42,
    ]) {
      expect(
        parseSavedPublicAppearance({ ...saved, branding: { ...saved.branding, contactUrl } }),
      ).toBeNull();
    }
  });

  it('rejects a documentation link that is not an http(s) URL', () => {
    for (const docsUrl of ['javascript:alert(1)', 'docs.example.com', 42]) {
      expect(
        parseSavedPublicAppearance({ ...saved, branding: { ...saved.branding, docsUrl } }),
      ).toBeNull();
    }
  });

  it('allows only validated public display fields into the server fallback', () => {
    expect(
      parseSavedPublicAppearance({
        ...saved,
        deployment: { version: 'foreign-image' },
        agent_access: { allowed: true },
        secret: 'must-never-be-published',
      }),
    ).toEqual({
      branding: { ...saved.branding, gatewayUrl: 'https://gateway.example.com' },
      features: saved.features,
    });
    expect(parseSavedPublicAppearance({})).toBeNull();
    expect(
      parseSavedPublicAppearance({
        ...saved,
        branding: { ...saved.branding, gatewayUrl: 'javascript:alert(1)' },
      }),
    ).toBeNull();
  });

  it('uses saved branding and switches while retaining only deployment metadata from the server', () => {
    const site = parseSavedSiteConfig(
      { ...saved, secret: 'must-never-be-published' },
      {
        AGENT_WEB_APP_NAME: 'Old environment name',
        AGENT_WEB_FEATURE_RAG: 'false',
        AGENT_DEPLOYMENT_SHA: 'image-version',
        AGENT_WEB_DEPLOYED_AT: '2026-10-10T12:00:00Z',
      },
    );
    expect(site.branding.appName).toBe('Admin configured app');
    expect(site.branding.gatewayUrl).toBe('https://gateway.example.com');
    expect(site.features.rag).toBe(true);
    expect(site.deployment).toEqual({
      version: 'image-version',
      deployedAt: '2026-10-10T12:00:00.000Z',
    });
    expect(JSON.stringify(site)).not.toContain('must-never-be-published');
    expect(JSON.stringify(site)).not.toContain('Old environment name');
  });

  it('preserves deliberate empty links instead of returning to environment settings', () => {
    const site = parseSavedSiteConfig(
      { ...saved, branding: { ...saved.branding, orgUrl: '' } },
      { AGENT_WEB_ORG_URL: 'https://old.example.com' },
    );
    expect(site.branding.orgUrl).toBe('');
  });

  it.each([
    null,
    {},
    { ...saved, features: { rag: 'true' } },
    { ...saved, branding: {} },
    { ...saved, branding: { ...saved.branding, orgUrl: 'https://exa mple.com' } },
    { ...saved, branding: { ...saved.branding, statusUrl: 'https://%' } },
    { ...saved, branding: { ...saved.branding, gatewayUrl: 'javascript:alert(1)' } },
  ])(
    'returns safe display defaults for malformed saved settings without restoring legacy branding',
    (value) => {
      const site = parseSavedSiteConfig(value, {
        AGENT_WEB_APP_NAME: 'Old environment branding',
        AGENT_WEB_ORG_URL: 'https://old.example.com',
        AGENT_WEB_FEATURE_RAG: 'true',
        AGENT_DEPLOYMENT_SHA: 'image-version',
      });
      expect(site).toEqual({
        ...NEUTRAL_SITE_CONFIG,
        deployment: { version: 'image-version', deployedAt: '' },
      });
      expect(JSON.stringify(site)).not.toContain('Old environment branding');
      expect(JSON.stringify(site)).not.toContain('old.example.com');
    },
  );
});
