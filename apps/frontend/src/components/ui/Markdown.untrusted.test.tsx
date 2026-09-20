// @vitest-environment jsdom
//
// The invariant the same-origin deployment rests on.
//
// This app is mounted at `/agents` under the gateway's own hostname, so it
// shares an origin with it — and the gateway keeps its access token in
// `sessionStorage`, which is scoped to an origin and nothing finer. A script
// that runs here can read it.
//
// A port was tried and does not work on this deployment: public traffic
// arrives through a Cloudflare Tunnel whose ingress rules match on hostname,
// so `:443` and `:8443` reach the same service. A distinct hostname is the
// only real separation available and this deployment has none.
//
// So the boundary is not the origin. It is this: **model output and terminal
// contents never become script.** Today they do not — react-markdown escapes
// raw HTML and strips dangerous URL schemes, and nothing in the app writes
// raw HTML. That is a property of the current dependency defaults rather than
// of anything we wrote, which is exactly why it is pinned here: the day
// someone adds `rehype-raw` for a legitimate-looking reason, this fails
// instead of quietly handing over the gateway's session.
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { Markdown } from '@/components/ui/Markdown';

describe('untrusted model output cannot become script', () => {
  it('escapes raw HTML rather than rendering it', () => {
    const { container } = render(<Markdown text={'<img src=x onerror="alert(1)">'} />);

    expect(container.querySelectorAll('img')).toHaveLength(0);
    expect(container.textContent).toContain('<img src=x');
  });

  it('does not execute a script tag', () => {
    const { container } = render(<Markdown text={'<script>alert(1)</script>'} />);

    expect(container.querySelectorAll('script')).toHaveLength(0);
  });

  it.each([
    ['javascript:alert(1)', 'javascript'],
    ['JaVaScRiPt:alert(1)', 'mixed-case javascript'],
    ['data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==', 'data: html'],
    ['vbscript:msgbox(1)', 'vbscript'],
  ])('neutralises %s (%s) in a link', (url) => {
    const { container } = render(<Markdown text={`[click](${url})`} />);

    const href = container.querySelector('a')?.getAttribute('href') ?? '';
    expect(href.toLowerCase()).not.toMatch(/^(javascript|data|vbscript):/);
  });

  it('still renders an ordinary link, so the check above is not vacuous', () => {
    const { container } = render(<Markdown text={'[docs](https://example.org/x)'} />);

    expect(container.querySelector('a')?.getAttribute('href')).toBe('https://example.org/x');
  });
});
