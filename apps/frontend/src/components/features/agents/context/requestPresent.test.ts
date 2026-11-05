import { describe, expect, it } from 'vitest';

import type { CompositionEntryApi } from '@/lib/api/agents';
import { filterEntries } from './requestPresent';

function entry(
  event_id: number | null,
  turn_no: number,
  category: string,
  label: string,
  preview = '',
): CompositionEntryApi {
  return {
    category,
    label,
    event_id,
    job_id: `j${turn_no}`,
    turn_no,
    chars: 10,
    tokens: 3,
    truncated: false,
    preview,
  };
}

// As the server sends them: largest first.
const ENTRIES = [
  entry(30, 2, 'tool_results', 'bash', 'npm test: 3 failed'),
  entry(12, 1, 'assistant', 'answer', 'I will run the tests'),
  entry(null, 2, 'user_messages', 'prompt', 'Fix the LOGIN redirect'),
  entry(11, 1, 'tool_calls', 'read_file', 'src/login.ts'),
];

describe('filterEntries', () => {
  it('searches names and previews without case, within a category', () => {
    expect(filterEntries(ENTRIES, null, 'login').map((e) => e.label)).toEqual([
      'prompt',
      'read_file',
    ]);
    expect(filterEntries(ENTRIES, 'tool_calls', 'login').map((e) => e.label)).toEqual([
      'read_file',
    ]);
    expect(filterEntries(ENTRIES, null, '  ')).toEqual(ENTRIES);
  });

  it('lists in transcript order on request, a turn’s prompt first', () => {
    expect(filterEntries(ENTRIES, null, '', 'transcript').map((e) => e.label)).toEqual([
      'read_file',
      'answer',
      'prompt',
      'bash',
    ]);
    // The server's order is kept otherwise.
    expect(filterEntries(ENTRIES, null)).toEqual(ENTRIES);
  });
});
