// @vitest-environment jsdom
import { afterEach, describe, expect, it } from 'vitest';
import {
  INVITATION_STASH_MAX_AGE_MS,
  clearInvitationToken,
  readInvitationToken,
  stashInvitationToken,
  tokenFromHash,
} from './teamInvitation';

// Every character class a link token can use, without looking like a real secret.
const TOKEN = `${'a'.repeat(10)}${'B'.repeat(10)}${'7'.repeat(10)}_-${'z'.repeat(11)}`;

describe('invitation token hand-off', () => {
  afterEach(() => window.sessionStorage.clear());

  it('reads only a well-formed token from the fragment', () => {
    expect(TOKEN).toHaveLength(43);
    expect(tokenFromHash(`#token=${TOKEN}`)).toBe(TOKEN);
    expect(tokenFromHash(`token=${TOKEN}`)).toBe(TOKEN);
    expect(tokenFromHash('#token=short')).toBeNull();
    expect(tokenFromHash(`#token=${TOKEN}<script>`)).toBeNull();
    expect(tokenFromHash('')).toBeNull();
  });

  it('keeps the token for this tab only, and only for a limited time', () => {
    stashInvitationToken(TOKEN, 1_000);
    expect(window.localStorage.length).toBe(0);
    expect(readInvitationToken(1_000 + INVITATION_STASH_MAX_AGE_MS)).toBe(TOKEN);
    expect(readInvitationToken(1_001 + INVITATION_STASH_MAX_AGE_MS)).toBeNull();
    // An expired entry is removed, not just ignored.
    expect(readInvitationToken(1_000)).toBeNull();
  });

  it('forgets a token once used and ignores tampered storage', () => {
    stashInvitationToken(TOKEN);
    clearInvitationToken();
    expect(readInvitationToken()).toBeNull();
    window.sessionStorage.setItem(
      'cloud-agent:team-invitation',
      JSON.stringify({ token: 'not-a-token', savedAt: Date.now() }),
    );
    expect(readInvitationToken()).toBeNull();
    window.sessionStorage.setItem('cloud-agent:team-invitation', '{broken');
    expect(readInvitationToken()).toBeNull();
  });
});
