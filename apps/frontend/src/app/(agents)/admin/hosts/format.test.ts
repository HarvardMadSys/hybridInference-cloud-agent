// The group-selection rules behind the Servers table.
//
// **A separate module because `page.tsx` may not export anything else.** These
// live beside `serverInventory` rather than in `capacity.ts` because they are
// about which *identity* a row is showing, not about what a capacity number
// means — the same split the file already makes for grouping.

import { describe, expect, it } from 'vitest';

import type { AgentRunnerHost } from '@/lib/api/admin';

import { curveSource, curveSourceHosts, hostStatusLabel } from './format';

function host(name: string, status = 'online'): AgentRunnerHost {
  return { host: name, status, labels: {} } as unknown as AgentRunnerHost;
}

describe('host status label', () => {
  it('names a drain by what it leads to, never as draining', () => {
    expect(hostStatusLabel({ status: 'draining', removal_pending: true })).toBe('removing');
    expect(hostStatusLabel({ status: 'draining', drain_fence_active: true })).toBe('updating');
    expect(hostStatusLabel({ status: 'draining' })).toBe('paused');
  });

  it('passes every other status through', () => {
    for (const status of ['enrolling', 'online', 'offline', 'disabled']) {
      expect(hostStatusLabel({ status })).toBe(status);
    }
  });
});

describe('curve sources', () => {
  it('never offers an enrollment that has been removed', () => {
    // `Remove` disables the row and revokes its credential. The row survives
    // on purpose — attempts point at it and its history stays in the API — but
    // it is not an environment anyone can read a curve from, and offering it
    // is what made this control look like the server's own name repeated.
    const members = [host('rtx6000b-kata'), host('rtx6000b', 'disabled')];

    expect(curveSourceHosts(members).map((member) => member.host)).toEqual(['rtx6000b-kata']);
  });

  it('drops an enrollment that stopped reporting while another one is', () => {
    // The same machine under a previous identity: a row the reaper marked
    // offline after a rename, which nothing ever cleans up. It is not a second
    // environment, and its curve is this machine's history from before the
    // rename — so leaving it out of a picker loses no reading.
    const members = [host('rtx6000b-kata', 'online'), host('rtx6000b', 'offline')];

    expect(curveSourceHosts(members).map((member) => member.host)).toEqual(['rtx6000b-kata']);
  });

  it('counts a draining enrollment as one that is still reporting', () => {
    // Draining is an operator's instruction to stop taking work, not silence:
    // the machine is still there and still sending its load.
    const members = [host('a', 'draining'), host('b', 'offline')];

    expect(curveSourceHosts(members).map((member) => member.host)).toEqual(['a']);
  });

  it('keeps every live enrollment when none of them is reporting', () => {
    // A machine that is rebooting has no reporting row at all. Its last curve
    // is still this machine's, and the stale marker is what says how old it
    // is — so the fallback is everyone who has not been removed.
    const members = [host('a', 'offline'), host('b', 'enrolling')];

    expect(curveSourceHosts(members).map((member) => member.host)).toEqual(['a', 'b']);
  });

  it('opens on the enrollment the row is already showing', () => {
    const members = [host('a'), host('b')];

    expect(curveSource(members, 'b', 'a')?.host).toBe('b');
  });

  it('falls back to the representative when that source has been removed', () => {
    // The row's CPU and memory can come from an enrollment that is removed a
    // moment later; the curve must not be left pointing at one the panel does
    // not offer.
    const members = [host('a'), host('b', 'disabled')];

    expect(curveSource(members, 'b', 'a')?.host).toBe('a');
  });

  it('falls back to the first live enrollment when there is no representative', () => {
    expect(curveSource([host('a'), host('b')], null, null)?.host).toBe('a');
  });

  it('has no source at all when every enrollment has been removed', () => {
    expect(curveSource([host('a', 'disabled')], 'a', 'a')).toBeNull();
    expect(curveSource([], null, null)).toBeNull();
  });
});
