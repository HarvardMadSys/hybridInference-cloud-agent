// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DEFAULT_PREFERENCES,
  manualOrder,
  moveBefore,
  readSidebarPreferences,
  sidebarPreferencesKey,
  useSidebarPreferences,
} from './sidebarPreferences';
beforeEach(() => localStorage.clear());
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});
describe('personal sidebar preferences', () => {
  it('persists explicitly added workspaces before manual order or any task exists', () => {
    const { result, rerender } = renderHook(({ id }) => useSidebarPreferences(id), {
      initialProps: { id: 'alice' },
    });
    act(() => result.current[1]((old) => ({ ...old, addedProjects: ['owner/empty'] })));
    const stored = JSON.parse(localStorage.getItem(sidebarPreferencesKey('alice'))!);
    expect(stored.addedProjects).toEqual(['owner/empty']);
    expect(stored.tasks).toBeUndefined();
    expect(stored.projects).toBeUndefined();
    rerender({ id: 'bob' });
    expect(result.current[0].addedProjects).toEqual([]);
    rerender({ id: 'alice' });
    expect(result.current[0].addedProjects).toEqual(['owner/empty']);
    expect(result.current[0].manualInitialized).toBe(false);
  });
  it('deduplicates added workspaces and discards malformed entries', () => {
    localStorage.setItem(
      sidebarPreferencesKey('alice'),
      JSON.stringify({ addedProjects: ['owner/repo', null, 'owner/repo', 7] }),
    );
    expect(readSidebarPreferences('alice').addedProjects).toEqual(['owner/repo']);
  });
  it('remembers separate account orders and restores without leaking the previous account', async () => {
    const { result, rerender } = renderHook(({ id }) => useSidebarPreferences(id), {
      initialProps: { id: 'alice' },
    });
    act(() =>
      result.current[1]((old) => ({
        ...old,
        group: 'flat',
        order: 'manual',
        tasks: { 'repo:owner/repo': ['b', 'a'] },
      })),
    );
    expect(readSidebarPreferences('alice').tasks).toEqual({ 'repo:owner/repo': ['b', 'a'] });
    rerender({ id: 'bob' });
    await waitFor(() => expect(result.current[0]).toEqual(DEFAULT_PREFERENCES));
    act(() => result.current[1]((old) => ({ ...old, order: 'manual', projects: ['other/repo'] })));
    expect(readSidebarPreferences('alice').group).toBe('flat');
    expect(readSidebarPreferences('bob').projects).toEqual(['other/repo']);
    rerender({ id: 'alice' });
    await waitFor(() => expect(result.current[0].tasks['repo:owner/repo']).toEqual(['b', 'a']));
  });
  it('tolerates malformed or blocked storage, including writes', () => {
    localStorage.setItem(sidebarPreferencesKey('bad'), '{broken');
    expect(readSidebarPreferences('bad')).toEqual(DEFAULT_PREFERENCES);
    localStorage.setItem(
      sidebarPreferencesKey('bad'),
      JSON.stringify({ group: 'invalid', manualInitialized: true, tasks: { repo: ['a', 3, 'a'] } }),
    );
    expect(readSidebarPreferences('bad').tasks.repo).toEqual(['a']);
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    const { result } = renderHook(() => useSidebarPreferences('blocked'));
    act(() => result.current[1]((old) => ({ ...old, group: 'flat' })));
    expect(result.current[0].group).toBe('flat');
  });
  it('moves only supplied peers and preserves remembered positions as new tasks appear', () => {
    expect(moveBefore(['a', 'b', 'c'], 'c', 'a')).toEqual(['c', 'a', 'b']);
    expect(moveBefore(['a', 'b'], 'other-repo', 'a')).toEqual(['a', 'b']);
    expect(manualOrder(['new', 'b', 'a'], ['a', 'b'], (id) => id)).toEqual(['a', 'b', 'new']);
  });
  it('retires unowned fold storage and removes default-order history without migrating it', () => {
    localStorage.setItem('agents.sidebar.projects', JSON.stringify({ 'private/old-owner': true }));
    localStorage.setItem(
      sidebarPreferencesKey('alice'),
      JSON.stringify({
        ...DEFAULT_PREFERENCES,
        group: 'flat',
        projects: ['owner/repo'],
        tasks: { flat: ['old-thread'] },
        expanded: { 'alice/repo': false },
      }),
    );
    const { result } = renderHook(() => useSidebarPreferences('alice'));
    expect(result.current[0]).toEqual({
      ...DEFAULT_PREFERENCES,
      group: 'flat',
      expanded: { 'alice/repo': false },
    });
    expect(localStorage.getItem('agents.sidebar.projects')).toBeNull();
    const saved = JSON.parse(localStorage.getItem(sidebarPreferencesKey('alice'))!);
    expect(saved.projects).toBeUndefined();
    expect(saved.tasks).toBeUndefined();
    expect(saved.expanded).not.toHaveProperty('private/old-owner');
  });
  it('preserves saved manual positions while displaying last updated', () => {
    const value = {
      ...DEFAULT_PREFERENCES,
      manualInitialized: true,
      projects: ['b/repo', 'a/repo'],
      tasks: { flat: ['b', 'a'] },
    };
    localStorage.setItem(sidebarPreferencesKey('alice'), JSON.stringify(value));
    const { result } = renderHook(() => useSidebarPreferences('alice'));
    expect(result.current[0]).toEqual(value);
    expect(JSON.parse(localStorage.getItem(sidebarPreferencesKey('alice'))!).tasks.flat).toEqual([
      'b',
      'a',
    ]);
  });
});
