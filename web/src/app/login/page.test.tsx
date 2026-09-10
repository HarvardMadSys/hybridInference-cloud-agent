// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

// The page exists so that a visitor is told what they are signing into, and so
// that a deployment whose sign-in is misconfigured has somewhere to say so.
// Both of those are what these cases check; the styling is not.

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

let search = '';

vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(search),
}));

// Spread the real module and override only what this test decides. A factory
// that lists exports goes stale the moment one is added.
vi.mock('@/config/env', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/config/env')>()),
  agentApiBase: 'https://agents.example.org/api',
}));

import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import type { RuntimeSiteConfig } from '@/config/site';
import LoginPage from './page';

// The deployment identity now arrives as a provider prop rather than from
// build-time environment, so the test supplies it the same way the root layout
// does. This is also what proves the page reads the runtime config and not a
// module-level constant.
const SITE_CONFIG: RuntimeSiteConfig = {
  branding: {
    appName: 'Cloud Agent',
    orgName: '',
    orgUrl: '',
    statusUrl: '',
    gatewayUrl: 'https://gateway.example.org',
  },
  features: { rag: false },
};

function renderLogin() {
  return render(
    <SiteConfigProvider config={SITE_CONFIG}>
      <LoginPage />
    </SiteConfigProvider>,
  );
}

describe('the login page', () => {
  afterEach(() => {
    search = '';
    cleanup();
  });

  it('names the authority, so nobody is sent to a stranger unannounced', async () => {
    renderLogin();

    expect(
      await screen.findByRole('link', { name: /sign in with gateway.example.org/i }),
    ).toBeInTheDocument();
    expect(screen.getByText('Cloud Agent')).toBeInTheDocument();
  });

  it('carries the page the visitor was trying to reach', async () => {
    search = 'return_to=%2Fajob_123%3Ftab%3Ddiff';

    renderLogin();

    const link = await screen.findByRole('link', { name: /sign in/i });
    const url = new URL(link.getAttribute('href') ?? '', 'https://agents.example.org');
    expect(url.pathname).toBe('/api/v1/session/login');
    expect(url.searchParams.get('return_to')).toBe('/ajob_123?tab=diff');
  });

  it('refuses to carry an off-site destination', async () => {
    // `return_to` reaches this page through a redirect chain, so it is
    // attacker-influenced. The control plane checks it again — that is the copy
    // that counts — but sending it is already wrong.
    search = 'return_to=https%3A%2F%2Fevil.example%2Fx';

    renderLogin();

    const link = await screen.findByRole('link', { name: /sign in/i });
    const url = new URL(link.getAttribute('href') ?? '', 'https://agents.example.org');
    expect(url.searchParams.get('return_to')).toBe('/');
  });

  it('explains an unconfigured deployment instead of offering a dead button', async () => {
    // The control plane sends the visitor here with this flag when it cannot
    // sign anyone in. Offering the button anyway would loop them straight back.
    search = 'sign_in_error=unavailable';

    renderLogin();

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Sign-in is not configured correctly for this deployment.',
    );
    expect(screen.queryByRole('link', { name: /sign in/i })).not.toBeInTheDocument();
  });

  it('offers a fresh sign-in attempt after an outage and preserves the destination', async () => {
    search = 'sign_in_error=temporary&return_to=%2Fajob_123%3Ftab%3Ddiff';
    renderLogin();
    expect(await screen.findByRole('alert')).toHaveTextContent('temporarily unavailable');
    const link = screen.getByRole('link', { name: 'Try again' });
    const url = new URL(link.getAttribute('href') ?? '', 'https://agents.example.org');
    expect(url.pathname).toBe('/api/v1/session/login');
    expect(url.searchParams.get('return_to')).toBe('/ajob_123?tab=diff');
    expect(url.searchParams.has('sign_in_error')).toBe(false);
  });

  it('explains account denial without sending the user back through sign-in', async () => {
    search = 'sign_in_error=account_unavailable';
    renderLogin();
    expect(await screen.findByRole('alert')).toHaveTextContent('This account cannot sign in');
    expect(screen.queryByRole('link', { name: /sign in|try again/i })).not.toBeInTheDocument();
  });

  it('starts a fresh flow for expired login without carrying the old code', async () => {
    search = 'sign_in_error=expired&return_to=%2Fajob_123&code=spent&state=old';
    renderLogin();
    expect(await screen.findByRole('alert')).toHaveTextContent('expired or is no longer valid');
    const target = new URL(
      screen.getByRole('link', { name: 'Start again' }).getAttribute('href') ?? '',
      'https://agents.example.org',
    );
    expect(target.pathname).toBe('/api/v1/session/login');
    expect([...target.searchParams.entries()]).toEqual([['return_to', '/ajob_123']]);
  });
});
