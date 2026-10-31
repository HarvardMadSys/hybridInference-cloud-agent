import { describe, expect, it } from 'vitest';

import type { CommandExecutionApi } from '@/lib/api/agents';
import { clip, cutoffLabel, durationLabel, isSlow, statusTone } from './commandsPresent';

function execution(overrides: Partial<CommandExecutionApi> = {}): CommandExecutionApi {
  return {
    id: '1:c1',
    job_id: 'j1',
    turn_no: 1,
    attempt_id: 1,
    attempt_no: 1,
    runtime: 'claude-code',
    tool: 'Bash',
    command: 'pytest -q',
    command_chars: 9,
    command_truncated: false,
    cwd: '/work',
    status: 'completed',
    exit_code: 0,
    tool_error: false,
    background: false,
    superseded: false,
    duration_ms: 1_200,
    duration_basis: 'recorded',
    at: '2026-10-30T09:00:00Z',
    result_at: '2026-10-30T09:00:01Z',
    event_id: 1,
    result_event_id: 2,
    output_preview: 'ok',
    output_chars: 2,
    output_truncated: false,
    program: 'pytest',
    parse_basis: 'executable',
    ...overrides,
  };
}

describe('durationLabel', () => {
  it('renders a recorded duration at a readable scale', () => {
    expect(durationLabel(0)).toBe('1ms');
    expect(durationLabel(840)).toBe('840ms');
    expect(durationLabel(5_300)).toBe('5.3s');
    expect(durationLabel(14_000)).toBe('14s');
    expect(durationLabel(65_000)).toBe('1m05s');
    expect(durationLabel(11 * 60_000 + 45_000)).toBe('11m45s');
  });

  it('never turns a missing duration into zero', () => {
    expect(durationLabel(null)).toBeNull();
    expect(durationLabel(Number.NaN)).toBeNull();
    expect(durationLabel(-1)).toBeNull();
  });
});

describe('cutoffLabel', () => {
  it('writes the server cutoff in the same units a reader checks', () => {
    expect(cutoffLabel(10_000)).toBe('10s');
    expect(cutoffLabel(60_000)).toBe('1m');
    expect(cutoffLabel(1_500)).toBe('1500ms');
  });
});

describe('isSlow', () => {
  it('includes the cutoff itself and excludes a missing duration', () => {
    expect(isSlow(execution({ duration_ms: 10_000 }), 10_000)).toBe(true);
    expect(isSlow(execution({ duration_ms: 9_999 }), 10_000)).toBe(false);
    expect(isSlow(execution({ duration_ms: null }), 10_000)).toBe(false);
  });
});

describe('clip', () => {
  it('keeps short text whole and marks clipped text', () => {
    expect(clip('abc', 10)).toBe('abc');
    expect(clip('abcdefghijkl', 5)).toBe('abcde…');
  });
});

describe('statusTone', () => {
  it('gives each state its own glyph', () => {
    const glyphs = (['running', 'completed', 'failed', 'unknown'] as const).map(
      (status) => statusTone(status).glyph,
    );
    expect(new Set(glyphs).size).toBe(4);
  });
});
