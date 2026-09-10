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
  gatewayUrl: 'https://gateway.example.org',
  agentApiBase: 'https://agents.example.org/api',
  config: { apiBase: '', appName: 'Cloud Agent' },
}));

import LoginPage from './page';

describe('the login page', () => {
  afterEach(() => {
    search = '';
    cleanup();
  });

  it('names the authority, so nobody is sent to a stranger unannounced', async () => {
    render(<LoginPage />);

    expect(
      await screen.findByRole('link', { name: /sign in with gateway.example.org/i }),
    ).toBeInTheDocument();
    expect(screen.getByText('Cloud Agent')).toBeInTheDocument();
  });

  it('carries the page the visitor was trying to reach', async () => {
    search = 'return_to=%2Fajob_123%3Ftab%3Ddiff';

    render(<LoginPage />);

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

    render(<LoginPage />);

    const link = await screen.findByRole('link', { name: /sign in/i });
    const url = new URL(link.getAttribute('href') ?? '', 'https://agents.example.org');
    expect(url.searchParams.get('return_to')).toBe('/');
  });

  it('explains an unconfigured deployment instead of offering a dead button', async () => {
    // The control plane sends the visitor here with this flag when it cannot
    // sign anyone in. Offering the button anyway would loop them straight back.
    search = 'sign_in_error=unavailable';

    render(<LoginPage />);

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Sign-in is not configured for this deployment.',
    );
    expect(screen.queryByRole('link', { name: /sign in/i })).not.toBeInTheDocument();
  });

  it('offers a fresh sign-in attempt after an outage and preserves the destination', async () => {
    search = 'sign_in_error=temporary&return_to=%2Fajob_123%3Ftab%3Ddiff';
    render(<LoginPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('temporarily unavailable');
    const link = screen.getByRole('link', { name: 'Try again' });
    const url = new URL(link.getAttribute('href') ?? '', 'https://agents.example.org');
    expect(url.pathname).toBe('/api/v1/session/login');
    expect(url.searchParams.get('return_to')).toBe('/ajob_123?tab=diff');
    expect(url.searchParams.has('sign_in_error')).toBe(false);
  });

  it('explains account denial without sending the user back through sign-in', async () => {
    search = 'sign_in_error=account_unavailable';
    render(<LoginPage />);
    expect(await screen.findByRole('alert')).toHaveTextContent('This account cannot sign in');
    expect(screen.queryByRole('link', { name: /sign in|try again/i })).not.toBeInTheDocument();
  });
});
