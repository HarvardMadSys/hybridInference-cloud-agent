import { createHash } from 'node:crypto';

import { NextRequest } from 'next/server';
import { describe, expect, it } from 'vitest';

import {
  contentSecurityPolicy,
  createNonce,
  themeBootScriptHash,
} from '@/config/contentSecurityPolicy';
import { themeBootScript } from '@/lib/theme';
import { middleware } from '@/middleware';

/** A policy as a map from directive to its sources. */
function directives(policy: string): Map<string, string[]> {
  return new Map(
    policy.split(';').map((directive) => {
      const [name, ...sources] = directive.trim().split(/\s+/);
      return [name, sources];
    }),
  );
}

const BASE = { nonce: 'bm9uY2U=', themeScriptHash: 'sha256-dGhlbWU=' };

describe('contentSecurityPolicy', () => {
  it('allows scripts by nonce and hash, never inline or evaluated code', () => {
    const scripts = directives(contentSecurityPolicy({ ...BASE, apiBase: '/api' })).get(
      'script-src',
    );

    expect(scripts).toEqual([
      "'self'",
      "'nonce-bm9uY2U='",
      "'sha256-dGhlbWU='",
      "'strict-dynamic'",
    ]);
  });

  it('refuses framing, plugins and a rewritten base URL', () => {
    const policy = directives(contentSecurityPolicy({ ...BASE, apiBase: '/api' }));

    expect(policy.get('frame-ancestors')).toEqual(["'none'"]);
    expect(policy.get('object-src')).toEqual(["'none'"]);
    expect(policy.get('base-uri')).toEqual(["'none'"]);
    expect(policy.get('form-action')).toEqual(["'self'"]);
    expect(policy.get('default-src')).toEqual(["'self'"]);
  });

  it('allows the images the app shows: avatars, previews and registry icons', () => {
    const images = directives(contentSecurityPolicy({ ...BASE, apiBase: '/api' })).get('img-src');

    expect(images).toEqual(["'self'", 'data:', 'blob:', 'https:']);
  });

  it.each(['', '/api', '/agents/api'])(
    'connects to its own origin for the prefix %j',
    (apiBase) => {
      expect(directives(contentSecurityPolicy({ ...BASE, apiBase })).get('connect-src')).toEqual([
        "'self'",
      ]);
    },
  );

  it('adds the control plane origin when the API is built cross-origin', () => {
    const policy = contentSecurityPolicy({ ...BASE, apiBase: 'https://api.example.org/agents' });

    expect(directives(policy).get('connect-src')).toEqual(["'self'", 'https://api.example.org']);
  });

  it('adds evaluated code only for next dev', () => {
    const development = directives(
      contentSecurityPolicy({ ...BASE, apiBase: '/api', development: true }),
    );

    expect(development.get('script-src')).toContain("'unsafe-eval'");
    expect(development.get('script-src')).not.toContain("'unsafe-inline'");
  });
});

describe('the policy inputs', () => {
  it('hashes exactly the theme boot script the root layout inlines', async () => {
    const expected = createHash('sha256').update(themeBootScript()).digest('base64');

    expect(await themeBootScriptHash()).toBe(`sha256-${expected}`);
  });

  it('draws a fresh 128-bit nonce Next can read back', () => {
    const first = createNonce();

    expect(Buffer.from(first, 'base64')).toHaveLength(16);
    expect(createNonce()).not.toBe(first);
    // Next's own pattern for the nonce it extracts from the request's policy.
    expect(`'nonce-${first}'`).toMatch(/^'nonce-([A-Za-z0-9+/_-]+={0,2})'$/);
  });
});

describe('middleware', () => {
  function nonceOf(policy: string | null): string | undefined {
    return directives(policy ?? '')
      .get('script-src')
      ?.map((source) => /^'nonce-(.+)'$/.exec(source)?.[1])
      .find(Boolean);
  }

  it('gives the response and the render the same policy, with a nonce of its own', async () => {
    const request = new NextRequest('http://localhost/agents/login', {
      // A client cannot choose the nonce the page is rendered with.
      headers: { 'content-security-policy': "script-src 'nonce-chosen'" },
    });

    const response = await middleware(request);
    const sent = response.headers.get('content-security-policy');
    // `NextResponse.next({ request })` hands the rewritten request headers to
    // the render through these, which is where Next reads the nonce from.
    const rendered = response.headers.get('x-middleware-request-content-security-policy');

    expect(sent).not.toBeNull();
    expect(rendered).toBe(sent);
    expect(nonceOf(sent)).toBeDefined();
    expect(nonceOf(sent)).not.toBe('chosen');
    expect(directives(sent ?? '').get('script-src')).toContain(`'${await themeBootScriptHash()}'`);
  });

  it('draws a different nonce for every request', async () => {
    const first = await middleware(new NextRequest('http://localhost/'));
    const second = await middleware(new NextRequest('http://localhost/'));

    expect(nonceOf(first.headers.get('content-security-policy'))).not.toBe(
      nonceOf(second.headers.get('content-security-policy')),
    );
  });
});
