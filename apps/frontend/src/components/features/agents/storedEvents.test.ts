import { describe, expect, it } from 'vitest';

import { isTruncated, pairToolCalls, record, text } from './storedEvents';

interface Row {
  kind: 'call' | 'result';
  key: string;
  n: number;
}

const keysOf = (row: Row) => (row.kind === 'call' ? { call: row.key } : { result: row.key });

describe('pairToolCalls', () => {
  it('pairs a key that one call and one result carry', () => {
    const answer: Row = { kind: 'result', key: 'a1:t1', n: 2 };
    const pairs = pairToolCalls([{ kind: 'call', key: 'a1:t1', n: 1 }, answer], keysOf);

    expect(pairs.ambiguous('a1:t1')).toBe(false);
    expect(pairs.resultOf('a1:t1')).toBe(answer);
  });

  it('pairs nothing for an id called twice or answered twice, and keeps every record', () => {
    const pairs = pairToolCalls<Row>(
      [
        { kind: 'call', key: 'a1:t1', n: 1 },
        { kind: 'call', key: 'a1:t1', n: 2 },
        { kind: 'result', key: 'a1:t1', n: 3 },
        { kind: 'call', key: 'a1:t2', n: 4 },
        { kind: 'result', key: 'a1:t2', n: 5 },
        { kind: 'result', key: 'a1:t2', n: 6 },
      ],
      keysOf,
    );

    expect(pairs.callsOf('a1:t1')).toBe(2);
    expect(pairs.ambiguous('a1:t1')).toBe(true);
    expect(pairs.resultOf('a1:t1')).toBeUndefined();
    expect(pairs.ambiguous('a1:t2')).toBe(true);
    expect(pairs.resultOf('a1:t2')).toBeUndefined();
    expect(pairs.resultsOf('a1:t2').map((row) => row.n)).toEqual([5, 6]);
  });

  it('keeps attempts apart, so a retry repeating its ids is not a repeat', () => {
    const pairs = pairToolCalls<Row>(
      [
        { kind: 'call', key: 'a1:t1', n: 1 },
        { kind: 'result', key: 'a1:t1', n: 2 },
        { kind: 'call', key: 'a2:t1', n: 3 },
        { kind: 'result', key: 'a2:t1', n: 4 },
      ],
      keysOf,
    );

    expect(pairs.resultOf('a1:t1')?.n).toBe(2);
    expect(pairs.resultOf('a2:t1')?.n).toBe(4);
  });

  it('answers for a key it never saw as unpaired, not ambiguous', () => {
    const pairs = pairToolCalls<Row>([], keysOf);

    expect(pairs.callsOf('a1:t1')).toBe(0);
    expect(pairs.resultsOf('a1:t1')).toEqual([]);
    expect(pairs.ambiguous('a1:t1')).toBe(false);
    expect(pairs.resultOf('a1:t1')).toBeUndefined();
  });
});

describe('payload readers', () => {
  it('read a record, text and truncation the one way every view does', () => {
    expect(record({ a: 1 })).toEqual({ a: 1 });
    expect(record([1])).toEqual({});
    expect(record('x')).toEqual({});
    expect(text(3)).toBe('3');
    expect(text(false)).toBe('false');
    expect(text(null)).toBe('');
    expect(text({ a: 1 })).toBe('{"a":1}');
    expect(isTruncated({ truncated: true })).toBe(true);
    expect(isTruncated({ truncation_notice: 'cut' })).toBe(true);
    expect(isTruncated({ truncated: 'yes' })).toBe(false);
  });
});
