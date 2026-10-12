// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';

import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { parseRuntimeSiteConfig } from '@/config/site';
import { DeploymentFooter } from './DeploymentFooter';

afterEach(cleanup);

it('shows the release version and deployment date with their full values accessible', () => {
  const sha = 'abcdef0123456789abcdef0123456789abcdef01';
  render(
    <SiteConfigProvider
      config={parseRuntimeSiteConfig({
        AGENT_DEPLOYMENT_SHA: sha,
        AGENT_WEB_DEPLOYED_AT: '2026-08-07T18:30:00Z',
      })}
    >
      <DeploymentFooter />
    </SiteConfigProvider>,
  );
  const footer = screen.getByRole('contentinfo');
  expect(footer).toHaveTextContent('Version: abcdef0');
  expect(footer).toHaveTextContent('Deployed: 2026-08-07 UTC · 2:30 PM EDT');
  expect(screen.getByTitle(sha)).toHaveTextContent('abcdef0');
  expect(screen.getByTitle('2026-08-07T18:30:00.000Z')).toHaveAttribute(
    'datetime',
    '2026-08-07T18:30:00.000Z',
  );
});

it('shows missing metadata honestly rather than inventing a deployment date', () => {
  render(<DeploymentFooter />);
  expect(screen.getByRole('contentinfo')).toHaveTextContent('Version: Not available');
  expect(screen.getByRole('contentinfo')).toHaveTextContent('Deployed: Not available');
});
