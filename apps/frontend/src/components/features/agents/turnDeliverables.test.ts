import { describe, expect, it } from 'vitest';
import type { AgentJobApi, AgentJobEventApi, AgentThreadEventApi } from '@/lib/api/agents';
import { recordedTurnOutputs } from './turnDeliverables';

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

  it('rejects quoted paths and bare change markers that cannot form an accurate action', () => {
    expect(
      recordedTurnOutputs(JOB, [event('diff --git "a/spaced file.ts" "b/spaced file.ts"')]),
    ).toBeNull();
    expect(recordedTurnOutputs(JOB, [event('diff --git a/file.ts b/file.ts')])).toBeNull();
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
