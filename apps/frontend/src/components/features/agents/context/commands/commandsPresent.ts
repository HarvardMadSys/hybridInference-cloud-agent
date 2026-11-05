// Pure presentation of a conversation's command executions. The server owns
// filtering, counts and sorting (so a chip and the list always describe the
// same set, including on later pages); this module only decides how a value
// that exists is written, and never turns a missing duration into a zero.

import type { CommandExecutionApi } from '@/lib/api/agents';

/** The filters the list offers, in the order its chips show them. */
export const COMMAND_FILTERS = ['all', 'failed', 'slow', 'background'] as const;
export const COMMAND_SORTS = ['latest', 'slowest'] as const;

/** Bound the rendered command text; the row's own expansion shows the rest. */
export const COMMAND_CLIP = 220;
export const OUTPUT_CLIP = 4_000;

export function clip(text: string, limit: number): string {
  return text.length > limit ? `${text.slice(0, limit)}…` : text;
}

/** A duration in ms at a readable scale, or null when none was recorded. */
export function durationLabel(ms: number | null): string | null {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return null;
  // A recorded zero is zero, not "1ms": the number exists and was measured.
  if (ms === 0) return '0ms';
  if (ms < 1_000) return `${Math.max(1, Math.round(ms))}ms`;
  if (ms < 60_000) {
    const seconds = ms / 1_000;
    return `${seconds < 10 ? seconds.toFixed(1).replace(/\.0$/, '') : Math.round(seconds)}s`;
  }
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1_000);
  return `${minutes}m${seconds.toString().padStart(2, '0')}s`;
}

/** A cutoff a reader can check at a round scale. */
export function cutoffLabel(ms: number): string {
  if (ms % 60_000 === 0) return `${ms / 60_000}m`;
  if (ms % 1_000 === 0) return `${ms / 1_000}s`;
  return `${ms}ms`;
}

export function isSlow(execution: CommandExecutionApi, cutoffMs: number): boolean {
  return execution.duration_ms != null && execution.duration_ms >= cutoffMs;
}

/** A status's glyph and tone, shared by the row and its badge. */
export function statusTone(status: CommandExecutionApi['status']): {
  glyph: string;
  className: string;
} {
  if (status === 'running') return { glyph: '◐', className: 'text-sky-600 dark:text-sky-400' };
  if (status === 'completed') {
    return { glyph: '✓', className: 'text-emerald-600 dark:text-emerald-400' };
  }
  if (status === 'failed') return { glyph: '✕', className: 'text-rose-600 dark:text-rose-400' };
  return { glyph: '⊘', className: 'text-muted' };
}
