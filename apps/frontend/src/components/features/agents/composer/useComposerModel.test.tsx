// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentConfigApi } from '@/lib/api/agents';
import { providedSource, TEST_PROVIDED } from '@/test/connectionConfig';
import { useComposerModel } from './useComposerModel';

vi.mock('@/components/providers', () => ({ useT: () => (key: string) => key }));

const PROVIDED = { model: 'model-1', connectionId: TEST_PROVIDED };

/** An owner whose default is a provided model with `used` of one request spent. */
function loaded(used: number, resetsAt: string): AgentConfigApi {
  return {
    repos: [],
    runtimes: ['codex'],
    github_connected: true,
    github_install_url: null,
    default_model: 'model-1',
    default_model_connection_id: TEST_PROVIDED,
    model_connections_enabled: true,
    model_connections: [],
    provided_sources: [providedSource({ 'model-1': [1, used] }, { resets_at: resetsAt })],
  };
}

function composer(config: AgentConfigApi) {
  const view = renderHook(() => useComposerModel(null, 'codex'));
  act(() => view.result.current.adoptModels(config));
  return view;
}

describe('a provided model used up for today, on a page left open', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-11-12T23:59:59Z'));
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it('holds Run until the count starts over, then runs with nothing reloaded', () => {
    const { result } = composer(loaded(1, '2026-11-13T00:00:00Z'));
    expect(result.current.missingModel).toBe(true);
    expect(result.current.choice.model).toBe('');
    expect(result.current.unavailableHint).toBe('modelSource.usedUpHint');
    expect(result.current.picker.disabledOptions).toEqual(['model-1']);

    // Midnight UTC passes with nobody touching the page.
    act(() => {
      vi.advanceTimersByTime(1000);
    });
    expect(result.current.missingModel).toBe(false);
    expect(result.current.choice).toEqual(PROVIDED);
    expect(result.current.requestFields).toEqual({
      model: 'model-1',
      model_connection_id: TEST_PROVIDED,
    });
    expect(result.current.unavailableHint).toBe('');
    expect(result.current.picker.disabledOptions ?? []).toEqual([]);
  });

  it('offers exactly what its picker shows, on a redraw the reset did not cause', () => {
    const { result, rerender } = composer(loaded(1, '2026-11-13T00:00:00Z'));
    // The clock moves, the timer has not fired: the tab was asleep.
    vi.setSystemTime(new Date('2026-11-13T00:00:01Z'));
    rerender();
    expect(result.current.picker.disabledOptions ?? []).toEqual([]);
    act(() => result.current.picker.onChange('model-1'));
    expect(result.current.choice).toEqual(PROVIDED);
    expect(result.current.missingModel).toBe(false);
  });

  it('waits out a reset further off than a timer can be set for', () => {
    const { result } = composer(loaded(1, '2999-01-01T00:00:00Z'));
    act(() => {
      vi.advanceTimersByTime(40 * 24 * 60 * 60 * 1000);
    });
    expect(result.current.missingModel).toBe(true);
    // One timer re-armed, not a loop of them firing at once.
    expect(vi.getTimerCount()).toBe(1);
  });

  it('sets no timer where nothing is provided', () => {
    composer({ ...loaded(0, '2026-11-13T00:00:00Z'), provided_sources: [] });
    expect(vi.getTimerCount()).toBe(0);
  });
});
