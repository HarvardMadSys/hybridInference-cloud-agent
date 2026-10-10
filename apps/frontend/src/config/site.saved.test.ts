import { describe, expect, it } from 'vitest';

import { NEUTRAL_SITE_CONFIG, parseSavedSiteConfig } from './site';

const saved = {
  branding: {
    appName: 'Admin configured app',
    orgName: 'Organization',
    orgUrl: 'https://example.com/about',
    statusUrl: '',
    gatewayUrl: 'https://gateway.example.com/dashboard',
  },
  features: { rag: true },
};

describe('saved public site settings', () => {
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
