// @vitest-environment node
import { describe, expect, it } from 'vitest';

import type {
  MeasurementRecord,
  MeasurementSummaryGroup,
  MeasurementSummaryResponse,
} from '@/lib/api/adminMeasurements';

import {
  ATTACHED_STAGE_NOTE,
  EMPTY_SELECTION,
  GAP_NAME,
  MEASUREMENTS_PATH,
  SERVER_SIDE_NOTE,
  TRUNCATED_STAGE_NOTE,
  coverageLine,
  coverageScope,
  duration,
  filterChips,
  isCompleteOutcome,
  measuredBetween,
  namesRecords,
  operationLabel,
  operationName,
  outcomeLabel,
  partialReasons,
  runLabel,
  sampleResult,
  selectionFromQuery,
  selectionHref,
  selectionQuery,
  shareOf,
  shortTime,
  stageName,
  timelineRows,
  timingRows,
} from './present';

function record(overrides: Partial<MeasurementRecord> = {}): MeasurementRecord {
  return {
    schema_version: 3,
    measurement_id: 'msm_1',
    interaction_id: 'msm_1',
    profiling_run_id: 'run-1',
    operation: 'files.list',
    boundary_version: 1,
    coverage_scope: 'server_request',
    end_to_end_complete: false,
    producer: 'control-plane',
    producer_instance_id: 'cp-1',
    outcome: 'completed',
    duration_us: 2500,
    collection_complete: true,
    observed_at: '2026-09-17T10:00:00Z',
    ...overrides,
  };
}

function group(overrides: Partial<MeasurementSummaryGroup> = {}): MeasurementSummaryGroup {
  return {
    group: {
      operation: 'files.list',
      request_kind: 'files.list',
      boundary_version: 1,
      coverage_scope: 'server_request',
      runtime_id: null,
      runtime_version: null,
      execution_profile: null,
      startup_path: null,
    },
    sample_count: 30,
    interaction_count: 12,
    eligible_count: 25,
    incomplete_count: 3,
    failed_count: 2,
    p50_us: 2500,
    p95_us: 9000,
    min_us: 500,
    max_us: 20000,
    ...overrides,
  };
}

function startupGroup(
  dims: Record<string, string | number | null>,
  overrides: Partial<MeasurementSummaryGroup> = {},
): MeasurementSummaryGroup {
  return group({
    group: {
      operation: 'attempt.startup',
      request_kind: 'attempt.startup',
      boundary_version: 1,
      coverage_scope: 'host_startup',
      runtime_id: 'claude-code',
      runtime_version: '2.1.220',
      execution_profile: 'untrusted-kata-v1',
      startup_path: 'new_group',
      ...dims,
    },
    ...overrides,
  });
}

describe('durations', () => {
  it('reads in the unit a reader compares in and keeps the microseconds exact', () => {
    expect(duration(12_401_792)).toEqual({ display: '12.40 s', exact: '12,401,792 µs' });
    expect(duration(7_034_534).display).toBe('7.03 s');
    expect(duration(325_559).display).toBe('326 ms');
    expect(duration(42_998).display).toBe('43 ms');
    expect(duration(5_515).display).toBe('5.5 ms');
    expect(duration(95_000_000).display).toBe('1 min 35 s');
  });

  it('shows a sub-millisecond duration in microseconds rather than as 0 ms', () => {
    // A stage that took 420 µs did not take zero milliseconds.
    expect(duration(420).display).toBe('420 µs');
    expect(duration(0).display).toBe('0 µs');
  });

  it('reports an unreported duration as unknown, never as zero', () => {
    expect(duration(null)).toEqual({ display: '—', exact: null });
    expect(duration(undefined)).toEqual({ display: '—', exact: null });
    expect(duration(Number.NaN)).toEqual({ display: '—', exact: null });
  });

  it('computes a share, and refuses one whose whole is zero or unknown', () => {
    expect(shareOf(250, 1000)).toBe('25%');
    expect(shareOf(1, 3)).toBe('33.3%');
    // A share of nothing is not 0% and not 100%: it is not computable.
    expect(shareOf(0, 0)).toBeNull();
    expect(shareOf(10, null)).toBeNull();
    expect(shareOf(null, 10)).toBeNull();
  });

  it('writes a short UTC time, and a dash for none', () => {
    expect(shortTime('2026-09-22T08:00:35Z')).toBe('Sep 22, 08:00 UTC');
    expect(shortTime(null)).toBe('—');
    expect(shortTime('not a time')).toBe('—');
  });
});

describe('names', () => {
  it('names an operation in an operator’s words, and an unknown one as it came', () => {
    expect(operationName('attempt.startup')).toBe('Sandbox startup');
    expect(operationName('terminal.open')).toBe('Terminal opening');
    expect(operationName('something.new')).toBe('something.new');
  });

  it('says which request and which startup path a sample is', () => {
    expect(
      operationLabel({
        operation: 'attempt.startup',
        request_kind: 'attempt.startup',
        startup_path: 'reuse_live',
        coverage_scope: 'host_startup',
      }),
    ).toBe('Sandbox startup · live sandbox reused');
    expect(
      operationLabel({
        operation: 'terminal.open',
        request_kind: 'terminal.stream_attach',
        coverage_scope: 'server_request',
      }),
    ).toBe('Terminal opening · attach stream');
    // A path the runner could not classify says so rather than borrowing one.
    expect(operationLabel({ operation: 'attempt.startup', startup_path: 'unknown' })).toBe(
      'Sandbox startup · path unclassified',
    );
    expect(operationLabel({ operation: 'files.list', request_kind: 'files.brand_new' })).toBe(
      'File listing · files.brand_new',
    );
  });

  it('adds a coverage scope only when it is one the page does not know', () => {
    // An unknown scope may cover more than the server, so it must not be read
    // under the page's server-side note.
    expect(operationLabel({ operation: 'files.list', coverage_scope: 'browser_interaction' })).toBe(
      'File listing · browser_interaction (unknown scope)',
    );
    expect(operationLabel({ operation: 'files.list', coverage_scope: 'server_request' })).toBe(
      'File listing',
    );
  });

  it('turns a recorded stage name into words', () => {
    expect(stageName('supervisor_acquire')).toBe('Supervisor acquire');
    expect(stageName('sandbox_manager.fetch')).toBe('Sandbox Manager fetch');
    expect(stageName('broker.fetch')).toBe('Sandbox Manager fetch');
    expect(stageName('claim')).toBe('Claim');
  });

  it('names a run by when it began producing and where', () => {
    expect(
      runLabel({ created_at: '2026-09-22T08:00:35Z', target: { host: 'rtx6000b-kata' } }),
    ).toBe('Sep 22, 08:00 UTC · rtx6000b-kata');
    expect(runLabel({ created_at: '2026-09-22T08:00:35Z', target: {} })).toBe(
      'Sep 22, 08:00 UTC · all hosts',
    );
  });

  it('carries the note every number on the page is read under', () => {
    expect(SERVER_SIDE_NOTE).toMatch(/server only/);
    expect(SERVER_SIDE_NOTE).toMatch(/none of these is the wait a user saw/);
  });
});

describe('outcome and completeness', () => {
  it('agrees with the server about which outcomes the percentiles include', () => {
    expect(isCompleteOutcome('completed')).toBe(true);
    expect(isCompleteOutcome('dispatched')).toBe(true);
    expect(isCompleteOutcome('failed')).toBe(false);
    expect(isCompleteOutcome('superseded')).toBe(false);
  });

  it('names a known outcome and refuses to invent one for an unknown word', () => {
    expect(outcomeLabel('lease_lost')).toBe('Lease lost');
    expect(outcomeLabel('something-new')).toBe('something-new (unrecognised outcome)');
    expect(outcomeLabel(null)).toBe('Outcome not reported');
  });

  it('says OK quietly and tells a failure from a partly measured success', () => {
    expect(sampleResult({ outcome: 'dispatched', collection_complete: true })).toMatchObject({
      label: 'OK',
      tone: 'ok',
    });
    expect(sampleResult({ outcome: 'failed', collection_complete: true })).toMatchObject({
      label: 'Failed',
      tone: 'bad',
    });
    // Collection that did not close is a different problem from a failure: the
    // operation worked, and its duration may stop short.
    const partial = sampleResult({ outcome: 'completed', collection_complete: false });
    expect(partial).toMatchObject({ label: 'Partly measured', tone: 'warn' });
    expect(partial.title).toMatch(/may stop short/);
    // A failed sample that also did not close is reported by its failure.
    expect(sampleResult({ outcome: 'cancelled', collection_complete: false }).label).toBe(
      'Cancelled',
    );
  });

  it('lists each reason a record is not whole, and nothing when it is', () => {
    expect(partialReasons(record())).toEqual([]);
    const reasons = partialReasons(
      record({
        collection_complete: false,
        outcome: 'failed',
        failure_stage: 'sandbox.start',
        truncated: true,
        truncated_fragments: ['sandbox_manager:call-1'],
        missing_fragments: ['sandbox_manager:call-2:sandbox.start'],
        missing_producers: ['broker'],
        dropped_spans: 2,
      }),
    );
    expect(reasons.join('\n')).toMatch(/Collection did not close/);
    expect(reasons.join('\n')).toMatch(/Failed at stage “sandbox.start”/);
    expect(reasons.join('\n')).toMatch(/floors/);
    expect(reasons.join('\n')).toMatch(/sandbox_manager:call-1/);
    expect(reasons.join('\n')).toMatch(/sandbox_manager:call-2:sandbox.start/);
    expect(reasons.join('\n')).toMatch(/never reported: Sandbox Manager/);
    expect(reasons.join('\n')).toMatch(/2 span\(s\) were dropped/);
  });
});

describe('coverage', () => {
  it('states what each known scope covers and what it leaves out', () => {
    expect(coverageScope('server_request').label).toBe('Server request');
    expect(coverageScope('host_startup').basis).toMatch(/Runner-rooted/);
    expect(coverageLine('server_request')).toMatch(/^Server request — /);
  });

  it('keeps an unknown or absent scope visibly unknown', () => {
    expect(coverageScope('browser_interaction').label).toBe('browser_interaction (unknown scope)');
    expect(coverageScope(null).label).toBe('Scope not reported');
  });

  it('says in words what a record was measured between', () => {
    expect(measuredBetween(record({ coverage_scope: 'host_startup' }))).toMatch(
      /runner claiming the turn to the prompt reaching the agent/,
    );
    expect(measuredBetween(record())).toMatch(/request being authorized/);
    expect(
      measuredBetween(
        record({ coverage_scope: 'new_scope', start_boundary: 'a.b', end_boundary: 'c.d' }),
      ),
    ).toBe('From a.b to c.d');
    expect(measuredBetween(record({ coverage_scope: 'new_scope' }))).toBeNull();
  });
});

describe('timeline', () => {
  // The shape staging records have: the claim is a span, the stages start
  // five seconds later, and nothing on the record says what happened between.
  const startup = record({
    operation: 'attempt.startup',
    coverage_scope: 'host_startup',
    duration_us: 7_000_000,
    spans: [
      {
        span_id: 's1',
        name: 'claim',
        ordinal: 1,
        start_offset_us: 30,
        duration_us: 300_000,
        outcome: 'ok',
      },
    ],
    stages: [
      { stage: 'validate', start_offset_us: 5_600_000, duration_us: 400_000 },
      {
        stage: 'supervisor_acquire',
        start_offset_us: 6_000_000,
        duration_us: 1_000_000,
        children: [
          { stage: 'container_start', duration_us: 900_000, source: 'broker', outcome: 'ok' },
          {
            stage: 'phase_network',
            duration_us: 80_000,
            source: 'broker',
            outcome: 'failed',
            truncated: true,
          },
        ],
      },
    ],
  });

  it('puts spans and stages in time order, with uncovered time as its own row', () => {
    const rows = timelineRows(startup);
    expect(rows.map((row) => [row.kind, row.name])).toEqual([
      ['span', 'Claim'],
      ['gap', GAP_NAME],
      ['stage', 'Validate'],
      ['stage', 'Supervisor acquire'],
      ['attached', 'Container start'],
      ['attached', 'Phase network'],
    ]);
    const gap = rows[1];
    expect(gap.startUs).toBe(300_030);
    expect(gap.durationUs).toBe(5_299_970);
    expect(gap.share).toBe('75.7%');
    expect(rows[2].share).toBe('5.7%');
  });

  it('keeps an attached duration off the timeline and out of the total', () => {
    const attached = timelineRows(startup).filter((row) => row.kind === 'attached');
    expect(attached.every((row) => row.startUs === null && row.share === null)).toBe(true);
    expect(attached[0]).toMatchObject({ depth: 1, source: 'broker', problem: null });
    // A failed or cut-short child says so; a successful one stays quiet.
    expect(attached[1]).toMatchObject({ problem: 'Failed', truncated: true });
    expect(ATTACHED_STAGE_NOTE).toMatch(/not added to the total/);
    expect(TRUNCATED_STAGE_NOTE).toMatch(/floor/);
  });

  it('adds a trailing gap, and none for boundary noise', () => {
    const rows = timelineRows(
      record({
        duration_us: 1_000_000,
        stages: [
          { stage: 'a', start_offset_us: 0, duration_us: 400_000 },
          // 2 µs between two stages is where one mark ends and the next begins.
          { stage: 'b', start_offset_us: 400_002, duration_us: 300_000 },
        ],
      }),
    );
    expect(rows.map((row) => row.kind)).toEqual(['stage', 'stage', 'gap']);
    expect(rows[2]).toMatchObject({ startUs: 700_002, durationUs: 299_998 });
  });

  it('lists a stage with no offset after the placed ones, without a position', () => {
    const rows = timelineRows(
      record({
        duration_us: 1_000,
        stages: [
          { stage: 'unplaced', duration_us: 100 },
          { stage: 'placed', start_offset_us: 0, duration_us: 1_000 },
        ],
      }),
    );
    expect(rows.map((row) => row.name)).toEqual(['Placed', 'Unplaced']);
    expect(rows[1]).toMatchObject({ startUs: null, share: null, durationUs: 100 });
  });

  it('nests a child span under its parent, and keeps an orphan as a root', () => {
    const rows = timelineRows(
      record({
        duration_us: 1_000,
        stages: [],
        spans: [
          {
            span_id: 'p',
            name: 'parent',
            ordinal: 1,
            start_offset_us: 0,
            duration_us: 1_000,
            outcome: 'ok',
          },
          {
            span_id: 'c',
            name: 'child',
            ordinal: 2,
            start_offset_us: 100,
            duration_us: 200,
            outcome: 'error',
            parent_span_id: 'p',
          },
          {
            span_id: 'o',
            name: 'orphan',
            ordinal: 3,
            start_offset_us: 500,
            duration_us: 100,
            outcome: 'ok',
            parent_span_id: 'gone',
          },
        ],
      }),
    );
    expect(rows.map((row) => [row.depth, row.name])).toEqual([
      [0, 'Parent'],
      [1, 'Child'],
      [0, 'Orphan'],
    ]);
    expect(rows[1].problem).toBe('Error');
  });

  it('shows a step recorded as both a stage and a span once', () => {
    // Since the preflight rework the runner records preflight both ways, over
    // the same interval.
    const rows = timelineRows(
      record({
        duration_us: 2_000_000,
        spans: [
          {
            span_id: 's1',
            name: 'claim',
            ordinal: 1,
            start_offset_us: 0,
            duration_us: 100_000,
            outcome: 'ok',
          },
          {
            span_id: 's2',
            name: 'preflight',
            ordinal: 1,
            start_offset_us: 200_010,
            duration_us: 799_980,
            outcome: 'failed',
          },
        ],
        stages: [
          { stage: 'preflight', start_offset_us: 200_000, duration_us: 800_000 },
          { stage: 'validate', start_offset_us: 1_000_000, duration_us: 1_000_000 },
        ],
      }),
    );
    expect(rows.map((row) => [row.kind, row.rawName])).toEqual([
      ['span', 'claim'],
      ['gap', null],
      ['stage', 'preflight'],
      ['stage', 'validate'],
    ]);
    // The span's problem is kept on the one row that remains.
    expect(rows[2].problem).not.toBeNull();
  });

  it('has no rows for a record with nothing on its timeline', () => {
    expect(timelineRows(record({ stages: undefined, spans: undefined }))).toEqual([]);
  });
});

describe('timings', () => {
  function response(groups: MeasurementSummaryGroup[]): MeasurementSummaryResponse {
    return { run_id: 'run-1', groups };
  }

  it('names the one reason a sample was left out', () => {
    const [failed] = timingRows(
      response([
        group({ sample_count: 1, eligible_count: 0, failed_count: 1, incomplete_count: 0 }),
      ]),
    );
    expect(failed.excluded?.label).toBe('1 failed');
    const [incomplete] = timingRows(
      response([
        group({ sample_count: 4, eligible_count: 3, failed_count: 0, incomplete_count: 1 }),
      ]),
    );
    expect(incomplete.excluded?.label).toBe('1 incomplete');
  });

  it('counts what was kept out of the percentiles instead of hiding it', () => {
    const [row] = timingRows(response([group()]));
    expect(row).toMatchObject({ label: 'File listing', detail: null, samples: 30, eligible: 25 });
    expect(row.excluded?.count).toBe(5);
    expect(row.excluded?.label).toBe('5 not counted');
    expect(row.excluded?.title).toMatch(/3 with collection incomplete, 2 not successful/);
    // The two exclusions are conditions, not categories: one sample can be both.
    expect(row.excluded?.title).toMatch(/one sample can be both/);
    expect([row.min.display, row.p50.display, row.p95.display, row.max.display]).toEqual([
      '500 µs',
      '2.5 ms',
      '9 ms',
      '20 ms',
    ]);
    expect(row.few).toBe(false);
  });

  it('renders a percentile with no eligible sample as unknown, never zero', () => {
    const [row] = timingRows(
      response([
        group({
          eligible_count: 0,
          p50_us: null,
          p95_us: null,
          min_us: null,
          max_us: null,
        }),
      ]),
    );
    expect(row.p50).toEqual({ display: '—', exact: null });
    expect(row.max.display).toBe('—');
    expect(row.few).toBe(true);
  });

  it('orders rows the way things happen rather than alphabetically', () => {
    const rows = timingRows(
      response([
        group({ group: { ...group().group, operation: 'files.list' } }),
        group({
          group: { ...group().group, operation: 'terminal.open', request_kind: 'terminal.create' },
        }),
        group({
          group: { ...group().group, operation: 'terminal.open', request_kind: 'terminal.list' },
        }),
        startupGroup({ startup_path: 'reuse_live' }),
        startupGroup({ startup_path: 'new_group' }),
      ]),
    );
    expect(rows.map((row) => row.label)).toEqual([
      'Sandbox startup · new sandbox',
      'Sandbox startup · live sandbox reused',
      'Terminal opening · list terminals',
      'Terminal opening · create terminal',
      'File listing',
    ]);
  });

  it('adds only the dimensions that actually tell an operation’s rows apart', () => {
    const rows = timingRows(
      response([
        startupGroup({ runtime_version: '2.1.220' }),
        startupGroup({ runtime_version: '2.1.221' }),
        group(),
      ]),
    );
    expect(rows.map((row) => row.detail)).toEqual([
      'claude-code 2.1.220',
      'claude-code 2.1.221',
      null,
    ]);
    // Every grouping dimension stays available, exactly as the server named it.
    expect(rows[0].title).toMatch(/execution_profile=untrusted-kata-v1/);
    expect(rows[2].title).toMatch(/startup_path=not reported/);
  });

  it('shows a dimension this page has never heard of rather than dropping it', () => {
    const rows = timingRows(
      response([
        group({ group: { ...group().group, region: 'eu' } }),
        group({ group: { ...group().group, region: 'us' } }),
      ]),
    );
    expect(rows.map((row) => row.detail)).toEqual(['region eu', 'region us']);
  });
});

describe('selection and links', () => {
  it('reads a full selection out of a URL, repeats included', () => {
    const { selection, error } = selectionFromQuery(
      'run=run-1&measurement=msm_1&operation=files.list&operation=files.open&request_kind=terminal.create' +
        '&coverage_scope=server_request&outcome=failed&collection_complete=false&job_id=ajob_1' +
        '&thread_id=athr_1&attempt_id=42&interaction_id=aint_1',
    );
    expect(error).toBeNull();
    expect(selection).toEqual({
      run: 'run-1',
      measurement: 'msm_1',
      operation: ['files.list', 'files.open'],
      request_kind: ['terminal.create'],
      coverage_scope: ['server_request'],
      outcome: 'failed',
      collection_complete: false,
      job_id: 'ajob_1',
      thread_id: 'athr_1',
      attempt_id: '42',
      interaction_id: 'aint_1',
    });
  });

  it('refuses a parameter it cannot express instead of widening the read', () => {
    expect(selectionFromQuery('run=run-1&host=host-a').error).toBe('Unsupported filter: host.');
  });

  it('refuses an attempt id that is not a number rather than reading every attempt', () => {
    for (const value of ['latest', '-1', '1.5', '99999999999999999999']) {
      expect(selectionFromQuery(`attempt_id=${value}`).error).toMatch(/not an attempt number/);
    }
  });

  it('refuses a collection_complete that is neither true nor false', () => {
    expect(selectionFromQuery('collection_complete=maybe').error).toMatch(/not true or false/);
  });

  it('treats an empty parameter as absent rather than as a name', () => {
    expect(selectionFromQuery('run=&job_id=').selection).toEqual(EMPTY_SELECTION);
  });

  it('opens an old paged link at its first page instead of failing', () => {
    // A cursor is a position, not a filter: dropping it widens nothing.
    const { selection, error } = selectionFromQuery('run=run-1&after=msm_9');
    expect(error).toBeNull();
    expect(selectionQuery(selection)).toBe('run=run-1');
  });

  it('writes a canonical query, repeats as repeats, and false as a value', () => {
    expect(
      selectionQuery({
        ...EMPTY_SELECTION,
        operation: ['files.list', 'files.open'],
        collection_complete: false,
        run: 'run-1',
      }),
    ).toBe('run=run-1&collection_complete=false&operation=files.list&operation=files.open');
    expect(selectionHref(MEASUREMENTS_PATH, EMPTY_SELECTION)).toBe(MEASUREMENTS_PATH);
    expect(selectionHref('/agents/admin/measurements', { ...EMPTY_SELECTION, job_id: 'j' })).toBe(
      '/agents/admin/measurements?job_id=j',
    );
  });

  it('knows a link that names records from one that names a run', () => {
    expect(namesRecords(EMPTY_SELECTION)).toBe(false);
    expect(namesRecords({ ...EMPTY_SELECTION, run: 'run-1', operation: ['files.list'] })).toBe(
      false,
    );
    expect(namesRecords({ ...EMPTY_SELECTION, attempt_id: '42' })).toBe(true);
    expect(namesRecords({ ...EMPTY_SELECTION, interaction_id: 'aint_1' })).toBe(true);
  });

  it('keeps every filter the menu does not show visible as a removable chip', () => {
    const chips = filterChips({
      ...EMPTY_SELECTION,
      operation: ['files.list'],
      job_id: 'ajob_1',
      attempt_id: '42',
      outcome: 'failed',
      collection_complete: false,
    });
    expect(chips.map((chip) => chip.label)).toEqual([
      'Turn ajob_1',
      'Startup #42',
      'Result Failed',
      'Partly measured only',
    ]);
    expect(chips[1].clear).toEqual({ attempt_id: null });
    // The operation has its own menu, so it is not a chip as well.
    expect(filterChips({ ...EMPTY_SELECTION, operation: ['files.list'] })).toEqual([]);
  });
});
