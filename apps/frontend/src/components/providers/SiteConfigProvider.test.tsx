// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// The provider is the seam between the server-resolved identity and the client
// shell. Two things have to hold: the config it is handed is what the shell
// reads, and a component rendered *outside* it shows the neutral identity
// rather than inheriting a real deployment's brand.

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { NEUTRAL_SITE_CONFIG, type RuntimeSiteConfig } from '@/config/site';
import { SiteConfigProvider, useSiteConfig } from './SiteConfigProvider';

function Probe() {
  const { branding } = useSiteConfig();
  return (
    <dl>
      <dd data-testid="app-name">{branding.appName}</dd>
      <dd data-testid="org-name">{branding.orgName}</dd>
      <dd data-testid="docs-url">{branding.docsUrl}</dd>
    </dl>
  );
}

const CONFIG: RuntimeSiteConfig = {
  branding: {
    appName: 'Acme Agents',
    orgName: 'Acme Research',
    orgUrl: 'https://acme.example',
    statusUrl: 'https://status.acme.example',
    docsUrl: 'https://docs.acme.example/agents/',
    contactUrl: 'mailto:support@acme.example',
  },
};

afterEach(cleanup);

describe('SiteConfigProvider', () => {
  it('hands the resolved config to the shell', () => {
    render(
      <SiteConfigProvider config={CONFIG}>
        <Probe />
      </SiteConfigProvider>,
    );

    expect(screen.getByTestId('app-name')).toHaveTextContent('Acme Agents');
    expect(screen.getByTestId('org-name')).toHaveTextContent('Acme Research');
    expect(screen.getByTestId('docs-url')).toHaveTextContent('https://docs.acme.example/agents/');
  });

  it('shows the neutral identity when no provider is above it', () => {
    // A route added above the root layout, a story or a stray render must not
    // inherit a name it was never given.
    render(<Probe />);

    expect(screen.getByTestId('app-name')).toHaveTextContent(NEUTRAL_SITE_CONFIG.branding.appName);
    expect(screen.getByTestId('docs-url')).toHaveTextContent('');
  });
});
