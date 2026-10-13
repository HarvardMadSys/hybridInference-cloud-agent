import { describe, expect, it } from 'vitest';

import {
  describeMcpAuthorizationFailure,
  describeProviderAuthorizationFailure,
} from './authorizationOutcome';

// Both return pages are reached by links anyone can write, so what they say
// has to be chosen by the outcome a redirect names, not copied out of it.

describe('describeMcpAuthorizationFailure', () => {
  it('shows the callback’s own reasons as they are', () => {
    for (const reason of [
      'GitHub sign-in was cancelled. You can try again.',
      'missing state or code',
      'this authorization expired, was already used, or was started by another account',
      'the connection changed during authorization',
    ]) {
      expect(describeMcpAuthorizationFailure(reason)).toBe(reason);
    }
  });

  it('words an authorization server’s error code itself', () => {
    expect(describeMcpAuthorizationFailure('access_denied')).toBe(
      'Sign-in did not complete: access was cancelled or denied (access_denied). Try again.',
    );
  });

  it('falls back to fixed text when there is no reason', () => {
    expect(describeMcpAuthorizationFailure(null)).toBe('Sign-in did not complete. Try again.');
    expect(describeMcpAuthorizationFailure('  ')).toBe('Sign-in did not complete. Try again.');
  });

  it('quotes anything else as unverified detail after the app’s own words', () => {
    expect(describeMcpAuthorizationFailure('the token endpoint answered 400')).toBe(
      'Sign-in did not complete. Try again. ' +
        'Unverified detail from the redirect: “the token endpoint answered 400”',
    );
  });

  it('shortens the detail and strips what could disguise it', () => {
    const spoof = `Your account is locked.\n‮evil‬ ${'Call support now. '.repeat(20)}`;
    const described = describeMcpAuthorizationFailure(spoof);
    const quoted = /“(.*)”$/.exec(described)?.[1] ?? '';

    expect(described.startsWith('Sign-in did not complete. Try again. ')).toBe(true);
    expect(Array.from(quoted)).toHaveLength(120);
    expect(quoted.endsWith('…')).toBe(true);
    expect(quoted).not.toMatch(/[\n‮‬]/);
  });

  it('adds no empty quotation when nothing printable is left', () => {
    expect(describeMcpAuthorizationFailure('\u202E\u202C')).toBe(
      'Sign-in did not complete. Try again.',
    );
  });

  it('does not treat an inherited property name as a known code', () => {
    expect(describeMcpAuthorizationFailure('constructor')).toContain('Unverified detail');
  });
});

describe('describeProviderAuthorizationFailure', () => {
  it('words a known error code and ignores the free-text description', () => {
    expect(
      describeProviderAuthorizationFailure(
        'GitHub',
        'access_denied',
        'Your GitHub account was suspended. Visit evil.example to restore it.',
      ),
    ).toBe('Access was cancelled or denied (access_denied).');
  });

  it('quotes an unknown provider error as unverified detail', () => {
    expect(describeProviderAuthorizationFailure('GitLab', 'odd_error', null)).toBe(
      'GitLab did not complete the authorization. ' +
        'Unverified detail from the redirect: “odd_error”',
    );
    expect(describeProviderAuthorizationFailure('GitHub', null, 'Something went wrong')).toBe(
      'GitHub did not complete the authorization. ' +
        'Unverified detail from the redirect: “Something went wrong”',
    );
  });
});
