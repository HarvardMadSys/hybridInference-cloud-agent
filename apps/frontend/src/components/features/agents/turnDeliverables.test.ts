import { describe, expect, it } from 'vitest';
import type { AgentJobApi, AgentJobEventApi, AgentThreadEventApi } from '@/lib/api/agents';
import { createRecordedOutputsSelector, recordedTurnOutputs } from './recordedTurnOutputs';

const JOB: Pick<AgentJobApi, 'id' | 'state' | 'published_pr_url' | 'current_attempt_id'> = {
  id: 'old-turn',
  state: 'succeeded',
  current_attempt_id: 2,
  published_pr_url: null,
};

function patch(path = 'src/app.tsx'): string {
  return [
    `diff --git a/${path} b/${path}`,
    'index 111..222 100644',
    `--- a/${path}`,
    `+++ b/${path}`,
    '@@ -1,2 +1,2 @@',
    ' context',
    '-old',
    '+new',
    '',
  ].join('\n');
}

function event(text: string, seq = 1): AgentJobEventApi {
  return {
    id: seq,
    attempt_id: 2,
    seq,
    event_type: 'diff',
    payload: { text },
    created_at: null,
  };
}

describe('recorded turn outputs', () => {
  it('keeps the producing turn identity and exact recorded file lines', () => {
    const outputs = recordedTurnOutputs(JOB, [event(patch())]);
    expect(outputs).toEqual({
      jobId: 'old-turn',
      files: [
        expect.objectContaining({
          path: 'src/app.tsx',
          add: 1,
          del: 1,
          lines: expect.arrayContaining([
            { marker: 'add', text: '+new' },
            { marker: 'del', text: '-old' },
          ]),
        }),
      ],
    });
  });

  it('does not attach another turn or a superseded attempt to a historical result', () => {
    const foreign: AgentThreadEventApi = {
      ...event(patch('later-turn.tsx'), 5),
      job_id: 'later-turn',
      turn_no: 2,
    };
    const superseded = { ...event(patch('superseded.tsx'), 8), attempt_id: 1 };
    expect(recordedTurnOutputs(JOB, [foreign, superseded, event(patch())])?.files).toEqual([
      expect.objectContaining({ path: 'src/app.tsx' }),
    ]);
    expect(recordedTurnOutputs(JOB, [foreign, superseded])).toBeNull();
  });

  it('selects the newest snapshot by stored sequence and does not combine incremental snapshots', () => {
    const outputs = recordedTurnOutputs(JOB, [
      event(patch('newest.tsx'), 3),
      event(patch('earlier.tsx'), 1),
    ]);
    expect(outputs?.files.map((file) => file.path)).toEqual(['newest.tsx']);
  });

  it('does not resurrect an earlier diff when the latest native snapshot has no patch', () => {
    const marker = { ...event('', 3), payload: { changed: true } };
    expect(recordedTurnOutputs(JOB, [event(patch()), marker])).toBeNull();
  });

  it.each(['queued', 'running', 'publishing'] as const)(
    'does not advertise outputs while a turn is %s',
    (state) => {
      expect(
        recordedTurnOutputs({ ...JOB, state, published_pr_url: 'https://github.com/a/b/pull/1' }, [
          event(patch()),
        ]),
      ).toBeNull();
    },
  );

  it('keeps direct recorded PRs usable when no native file diff exists', () => {
    expect(
      recordedTurnOutputs({ ...JOB, published_pr_url: 'https://github.com/a/b/pull/1' }, []),
    ).toEqual({ jobId: 'old-turn', files: [], prUrl: 'https://github.com/a/b/pull/1' });
  });

  it('ignores prose, generated artifact guesses, and unsafe link schemes', () => {
    expect(
      recordedTurnOutputs({ ...JOB, published_pr_url: 'javascript:alert(1)' }, [
        { ...event('Created src/app.tsx and report.png'), event_type: 'message' },
      ]),
    ).toBeNull();
  });

  it('rejects clipped previews even when an early hunk looks complete', () => {
    expect(recordedTurnOutputs(JOB, [event(patch().padEnd(4000, ' '))])).toBeNull();
    expect(
      recordedTurnOutputs(JOB, [
        { ...event(patch()), payload: { text: patch(), truncated: true } },
      ]),
    ).toBeNull();
    expect(recordedTurnOutputs(JOB, [event(patch().replace('+new\n', ''))])).toBeNull();
  });

  it('rejects bare change markers that cannot form an accurate action', () => {
    expect(
      recordedTurnOutputs(JOB, [event('diff --git "a/spaced file.ts" "b/spaced file.ts"')]),
    ).toBeNull();
    expect(recordedTurnOutputs(JOB, [event('diff --git a/file.ts b/file.ts')])).toBeNull();
  });

  it('names a file Git quoted by its decoded path, wherever it is listed', () => {
    const quoted = [
      'diff --git "a/\\346\\226\\207\\346\\241\\243.md" "b/\\346\\226\\207\\346\\241\\243.md"',
      'index ce01362..3b18e51 100644',
      '--- "a/\\346\\226\\207\\346\\241\\243.md"',
      '+++ "b/\\346\\226\\207\\346\\241\\243.md"',
      '@@ -1 +1 @@',
      '-hello',
      '+hello world',
      '',
    ].join('\n');
    expect(recordedTurnOutputs(JOB, [event(`${patch()}${quoted}`)])?.files).toEqual([
      expect.objectContaining({ path: 'src/app.tsx', add: 1, del: 1 }),
      expect.objectContaining({ path: '文档.md', add: 1, del: 1 }),
    ]);
    expect(recordedTurnOutputs(JOB, [event(`${quoted}${patch()}`)])?.files).toEqual([
      expect.objectContaining({ path: '文档.md', add: 1, del: 1 }),
      expect.objectContaining({ path: 'src/app.tsx', add: 1, del: 1 }),
    ]);
  });

  it('rejects ambiguous paths and repeated file snapshots rather than opening the wrong file', () => {
    expect(recordedTurnOutputs(JOB, [event(patch('folder b/nested/file.ts'))])).toBeNull();
    expect(recordedTurnOutputs(JOB, [event(`${patch()}${patch()}`)])).toBeNull();
  });

  it('retains binary changes without inventing zero additions or removals', () => {
    const binary = [
      'diff --git a/artifact.png b/artifact.png',
      'index 111..222 100644',
      'Binary files a/artifact.png and b/artifact.png differ',
    ].join('\n');
    expect(recordedTurnOutputs(JOB, [event(binary)])?.files).toEqual([
      { path: 'artifact.png', lines: [{ marker: 'ctx', text: binary.split('\n')[2] }] },
    ]);
  });

  it('does not misattribute attempt-scoped output when attempt identity is unknown', () => {
    expect(recordedTurnOutputs({ ...JOB, current_attempt_id: null }, [event(patch())])).toBeNull();
  });
});

describe('recorded outputs selector', () => {
  it('matches per-turn selection for labelled and unlabelled events, including equal sequences', () => {
    const selector = createRecordedOutputsSelector();
    const jobs = [
      JOB,
      { ...JOB, id: 'later-turn' },
      { ...JOB, current_attempt_id: null, id: 'unknown' },
    ];
    const labelled: AgentThreadEventApi = {
      ...event(patch('labelled.tsx'), 5),
      job_id: JOB.id,
      turn_no: 1,
    };
    const unlabelled = event(patch('unlabelled.tsx'), 5);
    const foreign: AgentThreadEventApi = {
      ...event(patch('foreign.tsx'), 8),
      job_id: 'foreign',
      turn_no: 3,
    };
    const superseded = { ...event(patch('superseded.tsx'), 9), attempt_id: 1 };

    for (const events of [
      [labelled, unlabelled, foreign, superseded],
      [unlabelled, labelled, foreign, superseded],
    ]) {
      expect(selector.select(jobs, events)).toEqual(
        new Map(jobs.map((job) => [job.id, recordedTurnOutputs(job, events)])),
      );
    }
    expect(selector.select(jobs, [labelled, unlabelled]).get(JOB.id)?.files[0].path).toBe(
      'labelled.tsx',
    );
    expect(selector.select(jobs, [unlabelled, labelled]).get(JOB.id)?.files[0].path).toBe(
      'unlabelled.tsx',
    );
  });

  it('keeps historical output, file, and line references when unrelated events stream', () => {
    const selector = createRecordedOutputsSelector();
    const diff: AgentThreadEventApi = { ...event(patch()), job_id: JOB.id, turn_no: 1 };
    const original = selector.select([JOB], [diff]).get(JOB.id);
    const message = { ...event('streaming', 2), event_type: 'message' };
    const foreign: AgentThreadEventApi = {
      ...event(patch('foreign.tsx'), 3),
      job_id: 'other-turn',
      turn_no: 2,
    };
    const updated = selector.select([{ ...JOB }], [diff, message, foreign]).get(JOB.id);

    expect(updated).toBe(original);
    expect(updated?.files).toBe(original?.files);
    expect(updated?.files[0]).toBe(original?.files[0]);
    expect(updated?.files[0].lines).toBe(original?.files[0].lines);
  });

  it('refreshes a new latest snapshot and a replacement event with the same stored id', () => {
    const selector = createRecordedOutputsSelector();
    const first = event(patch('first.tsx'));
    const original = selector.select([JOB], [first]).get(JOB.id);
    const latest = event(patch('latest.tsx'), 2);
    const updated = selector.select([JOB], [first, latest]).get(JOB.id);
    const replacement = { ...latest, payload: { text: patch('replacement.tsx') } };
    const replaced = selector.select([JOB], [first, replacement]).get(JOB.id);

    expect(updated).not.toBe(original);
    expect(updated?.files[0].path).toBe('latest.tsx');
    expect(replaced).not.toBe(updated);
    expect(replaced?.files).not.toBe(updated?.files);
    expect(replaced?.files[0].path).toBe('replacement.tsx');
    expect(selector.select([JOB], [first]).get(JOB.id)?.files[0].path).toBe('first.tsx');
  });

  it('refreshes the selected attempt and never falls back to a superseded attempt', () => {
    const selector = createRecordedOutputsSelector();
    const oldDiff = event(patch('old.tsx'));
    const newDiff = { ...event(patch('new.tsx'), 2), attempt_id: 3 };
    const retried = { ...JOB, current_attempt_id: 3 };

    expect(selector.select([JOB], [oldDiff, newDiff]).get(JOB.id)?.files[0].path).toBe('old.tsx');
    expect(selector.select([retried], [oldDiff]).get(JOB.id)).toBeNull();
    expect(selector.select([retried], [oldDiff, newDiff]).get(JOB.id)?.files[0].path).toBe(
      'new.tsx',
    );
    expect(
      selector.select([{ ...JOB, current_attempt_id: null }], [oldDiff, newDiff]).get(JOB.id),
    ).toBeNull();
  });

  it('refreshes PR and state values while reusing the parsed historical files', () => {
    const selector = createRecordedOutputsSelector();
    let patchReads = 0;
    const diff = {
      ...event(''),
      payload: {
        get text() {
          patchReads += 1;
          return patch();
        },
      },
    };
    const original = selector.select([JOB], [diff]).get(JOB.id);
    const withPr = selector
      .select([{ ...JOB, published_pr_url: 'https://github.com/a/b/pull/1' }], [diff])
      .get(JOB.id);
    expect(withPr).not.toBe(original);
    expect(withPr?.prUrl).toBe('https://github.com/a/b/pull/1');
    expect(withPr?.files).toBe(original?.files);

    const changedPr = selector
      .select([{ ...JOB, published_pr_url: 'https://github.com/a/b/pull/2' }], [diff])
      .get(JOB.id);
    expect(changedPr?.prUrl).toBe('https://github.com/a/b/pull/2');
    expect(changedPr?.files).toBe(original?.files);
    const unsafePr = selector
      .select([{ ...JOB, published_pr_url: 'javascript:alert(1)' }], [diff])
      .get(JOB.id);
    expect(unsafePr?.prUrl).toBeUndefined();
    expect(unsafePr?.files).toBe(original?.files);

    expect(selector.select([{ ...JOB, state: 'running' }], [diff]).get(JOB.id)).toBeNull();
    const settled = selector.select([{ ...JOB, state: 'failed' }], [diff]).get(JOB.id);
    expect(settled).not.toBe(original);
    expect(settled?.files).toBe(original?.files);
    expect(patchReads).toBe(1);
  });

  it('parses a diff only after the turn settles and evicts removed jobs from the output cache', () => {
    const selector = createRecordedOutputsSelector();
    let patchReads = 0;
    const diff = {
      ...event(''),
      payload: {
        get text() {
          patchReads += 1;
          return patch();
        },
      },
    };
    expect(selector.select([{ ...JOB, state: 'running' }], [diff]).get(JOB.id)).toBeNull();
    expect(patchReads).toBe(0);
    const settled = selector.select([JOB], [diff]).get(JOB.id);
    expect(patchReads).toBe(1);
    expect(selector.select([], [diff]).size).toBe(0);
    const returned = selector.select([JOB], [diff]).get(JOB.id);
    expect(returned).not.toBe(settled);
    expect(returned?.files).toBe(settled?.files);
    expect(patchReads).toBe(1);
  });

  it('uses only each producing job’s PR when no attempt-scoped diff is available', () => {
    const selector = createRecordedOutputsSelector();
    const published = {
      ...JOB,
      current_attempt_id: null,
      published_pr_url: 'https://github.com/a/b/pull/1',
    };
    const unpublished = { ...JOB, id: 'unpublished' };
    const diff: AgentThreadEventApi = { ...event(patch()), job_id: JOB.id, turn_no: 1 };
    const results = selector.select([published, unpublished], [diff]);
    expect(results.get(JOB.id)).toEqual({
      jobId: JOB.id,
      files: [],
      prUrl: 'https://github.com/a/b/pull/1',
    });
    expect(results.get(unpublished.id)).toBeNull();
  });

  it.each([
    { text: patch().padEnd(4000, ' ') },
    { text: patch(), truncated: true },
    { text: patch().replace('+new\n', '') },
    { text: 'diff --git "a/spaced file.ts" "b/spaced file.ts"' },
    { text: patch('folder b/nested/file.ts') },
    { text: `${patch()}${patch()}` },
    { changed: true },
  ])(
    'does not restore an earlier valid diff when the latest payload is rejected: %j',
    (payload) => {
      const selector = createRecordedOutputsSelector();
      const first = event(patch());
      expect(selector.select([JOB], [first]).get(JOB.id)).not.toBeNull();
      expect(selector.select([JOB], [first, { ...event('', 2), payload }]).get(JOB.id)).toBeNull();
    },
  );

  it('scans a large log once and does not reparse settled diffs after a stream append', () => {
    const selector = createRecordedOutputsSelector();
    const jobs = Array.from({ length: 200 }, (_, index) => ({ ...JOB, id: `turn-${index}` }));
    let eventReads = 0;
    let patchReads = 0;
    const events = jobs.flatMap((job, turn) =>
      Array.from({ length: 30 }, (_, index): AgentThreadEventApi => {
        const isDiff = index === 29;
        let payload: AgentJobEventApi['payload'] = null;
        if (isDiff) {
          payload = {
            get text() {
              patchReads += 1;
              return patch(`file-${turn}.tsx`);
            },
          };
        }
        return {
          ...event('', turn * 30 + index + 1),
          job_id: job.id,
          turn_no: turn + 1,
          get event_type() {
            eventReads += 1;
            return isDiff ? 'diff' : 'message';
          },
          payload,
        };
      }),
    );

    const previous = new Map(jobs.map((job) => [job.id, recordedTurnOutputs(job, events)]));
    expect(eventReads).toBe(1200000);
    expect(patchReads).toBe(200);
    eventReads = 0;
    patchReads = 0;

    const historical = selector.select(jobs, events);
    expect(eventReads).toBe(6000);
    expect(patchReads).toBe(200);
    expect(historical).toEqual(previous);

    eventReads = 0;
    patchReads = 0;
    const streamEvent = {
      ...event('streaming', 6001),
      get event_type() {
        eventReads += 1;
        return 'message';
      },
    };
    const streaming = selector.select(
      jobs.map((job) => ({ ...job })),
      [...events, streamEvent],
    );
    expect(eventReads).toBe(6001);
    expect(patchReads).toBe(0);
    for (const job of jobs) expect(streaming.get(job.id)).toBe(historical.get(job.id));
  });
});
