// @vitest-environment node
import { describe, expect, it } from 'vitest';

import type {
  MeasurementPage,
  MeasurementRecord,
  MeasurementSummaryGroup,
  MeasurementSummaryResponse,
} from '@/lib/api/adminMeasurements';

import {
  ATTACHED_STAGE_NOTE,
  DISTRIBUTION_BASIS,
  EMPTY_BASIS,
  EMPTY_SELECTION,
  MEASUREMENTS_PATH,
  MIN_PERCENTILE_SAMPLES,
  SERVER_ONLY_NOTICE,
  TRUNCATED_STAGE_NOTE,
  completenessLines,
  coverageLine,
  coverageScope,
  detailHeading,
  dimensionValue,
  duration,
  emptyPageNotice,
  factLines,
  factValue,
  groupBasis,
  groupRows,
  isCompleteOutcome,
  isFiltered,
  listNotice,
  outcomeLabel,
  partialReasons,
  rowCompleteness,
  rowIsPartial,
  sampleCaution,
  selectionFromForm,
  selectionFromQuery,
  selectionHref,
  selectionSummary,
  shareOf,
  splitValues,
  stageRows,
  summaryTotals,
  targetLines,
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

function page(overrides: Partial<MeasurementPage> = {}): MeasurementPage {
  return { measurements: [], count: 0, limit: 50, next_cursor: null, ...overrides };
}

describe('durations', () => {
  it('keeps the recorded microseconds exact and rounds only the display', () => {
    expect(duration(123_456)).toEqual({ display: '123.46 ms', exact: '123,456 µs' });
  });

  it('shows a sub-millisecond duration in microseconds rather than as 0 ms', () => {
    // A stage that took 420 µs did not take zero milliseconds.
    expect(duration(420).display).toBe('420 µs');
    expect(duration(0).display).toBe('0 µs');
  });

  it('reports an unreported duration as unknown, never as zero', () => {
    expect(duration(null)).toEqual({ display: 'Unavailable', exact: null });
    expect(duration(undefined)).toEqual({ display: 'Unavailable', exact: null });
    expect(duration(Number.NaN)).toEqual({ display: 'Unavailable', exact: null });
  });

  it('computes a share, and refuses one whose whole is zero or unknown', () => {
    expect(shareOf(250, 1000)).toBe('25%');
    expect(shareOf(1, 3)).toBe('33.3%');
    // A share of nothing is not 0% and not 100%: it is not computable.
    expect(shareOf(0, 0)).toBeNull();
    expect(shareOf(10, null)).toBeNull();
    expect(shareOf(null, 10)).toBeNull();
  });
});

describe('stage rows', () => {
  it('flattens stages in recorded order with attached children under their parent', () => {
    const rows = stageRows(
      record({
        duration_us: 1000,
        stages: [
          { stage: 'claim', start_offset_us: 0, duration_us: 400 },
          {
            stage: 'sandbox',
            start_offset_us: 400,
            duration_us: 600,
            children: [
              { stage: 'docker.create', duration_us: 300, outcome: 'ok', source: 'broker' },
              { stage: 'image.pull', duration_us: 250, outcome: 'ok', source: 'broker' },
            ],
          },
        ],
      }),
    );
    expect(rows.map((row) => [row.depth, row.name])).toEqual([
      [0, 'claim'],
      [0, 'sandbox'],
      [1, 'docker.create'],
      [1, 'image.pull'],
    ]);
    expect(rows[0].share).toBe('40%');
    expect(rows[1].share).toBe('60%');
  });

  it('gives an attached child no share, because it is not on this timeline', () => {
    const rows = stageRows(
      record({
        duration_us: 1000,
        stages: [
          {
            stage: 'sandbox',
            start_offset_us: 0,
            duration_us: 1000,
            children: [
              // A child can outlast its parent: two producers, two clocks. The
              // page must not sum them or present its duration as a share.
              { stage: 'docker.create', duration_us: 4000, source: 'broker' },
            ],
          },
        ],
      }),
    );
    expect(rows[1].share).toBeNull();
    expect(rows[1].durationUs).toBe(4000);
    expect(rows[1].source).toBe('broker');
  });

  it('marks a truncated child as a floor and keeps its call id', () => {
    const rows = stageRows(
      record({
        stages: [
          {
            stage: 'sandbox',
            duration_us: 10,
            children: [
              {
                stage: 'image.pull',
                duration_us: 9,
                source: 'broker',
                call_id: 'call-7',
                truncated: true,
              },
            ],
          },
        ],
      }),
    );
    expect(rows[1].truncated).toBe(true);
    expect(rows[1].callId).toBe('call-7');
    expect(TRUNCATED_STAGE_NOTE).toMatch(/floor/);
    expect(ATTACHED_STAGE_NOTE).toMatch(/not part of the total/);
  });

  it('renders a record with no stages as no rows, not as a zero-length stage', () => {
    expect(stageRows(record({ stages: undefined }))).toEqual([]);
    expect(stageRows(record({ stages: [] }))).toEqual([]);
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

  it('keeps the two completeness questions separate and warns on both today', () => {
    const lines = completenessLines({ collection_complete: true, end_to_end_complete: false });
    expect(lines[0]).toMatchObject({ label: 'Collection', warn: false });
    expect(lines[1]).toMatchObject({ label: 'End to end', warn: true });
    expect(lines[1].value).toMatch(/server-side only/i);
  });

  it('lists each reason a record is not whole, and nothing when it is', () => {
    expect(partialReasons(record())).toEqual([]);
    const reasons = partialReasons(
      record({
        collection_complete: false,
        outcome: 'failed',
        failure_stage: 'sandbox.start',
        truncated: true,
        truncated_fragments: ['broker:call-1'],
        missing_fragments: ['broker:call-2:sandbox.start'],
        missing_producers: ['broker'],
        dropped_spans: 2,
      }),
    );
    expect(reasons.join('\n')).toMatch(/Collection did not close/);
    expect(reasons.join('\n')).toMatch(/Failed at stage “sandbox.start”/);
    expect(reasons.join('\n')).toMatch(/floors/);
    expect(reasons.join('\n')).toMatch(/broker:call-1/);
    expect(reasons.join('\n')).toMatch(/broker:call-2:sandbox.start/);
    expect(reasons.join('\n')).toMatch(/never reported: broker/);
    expect(reasons.join('\n')).toMatch(/2 span\(s\) were dropped/);
  });

  it('never calls a summary row partial-complete: it can only say row is not whole', () => {
    expect(rowIsPartial({ collection_complete: true, outcome: 'completed' })).toBe(false);
    expect(rowIsPartial({ collection_complete: false, outcome: 'completed' })).toBe(true);
    expect(rowIsPartial({ collection_complete: true, outcome: 'failed' })).toBe(true);
    expect(rowCompleteness({ collection_complete: false, end_to_end_complete: false })).toBe(
      'Collection incomplete · Server-side only',
    );
  });
});

describe('coverage', () => {
  it('states what each known scope covers and what it leaves out', () => {
    expect(coverageScope('server_request').label).toBe('Server request');
    expect(coverageScope('server_request').basis).toMatch(/browser work are outside it/);
    expect(coverageScope('host_startup').label).toBe('Host startup');
    expect(coverageScope('host_startup').basis).toMatch(/Control-plane queueing/);
  });

  it('keeps an unknown or absent scope visibly unknown', () => {
    expect(coverageScope('browser_interaction')).toEqual({
      label: 'browser_interaction (unknown scope)',
      basis: expect.stringMatching(/not evidence of end-to-end coverage/),
    });
    expect(coverageScope(null).label).toBe('Scope not reported');
    expect(coverageLine(undefined)).toMatch(/Scope not reported/);
  });

  it('carries the server-only notice the whole page is read under', () => {
    expect(SERVER_ONLY_NOTICE).toMatch(/end_to_end_complete is false/);
    expect(SERVER_ONLY_NOTICE).toMatch(/Browser-to-server time is not measured/);
  });
});

describe('empty pages and paging', () => {
  it('uses the server wording and still names the four causes it cannot tell apart', () => {
    const notice = emptyPageNotice(
      page({ empty_reason: 'no stored samples match this selection' }),
    );
    expect(notice).toMatch(/^No stored samples match this selection\./);
    expect(notice).toMatch(/not zero latency/);
    expect(notice).toMatch(/switched off/);
    expect(notice).toMatch(/not be selected/);
    expect(notice).toMatch(/dropped/);
    expect(notice).toMatch(/older build/);
  });

  it('says the same thing when the server sent no reason at all', () => {
    expect(emptyPageNotice(page())).toBe(EMPTY_BASIS);
  });

  it('never lets a page stand in for the population', () => {
    const notice = listNotice(page({ count: 50, limit: 50, next_cursor: 'msm_50' }));
    expect(notice).toMatch(/50 of at most 50 rows/);
    expect(notice).toMatch(/not\s+the population/);
    expect(notice).toMatch(/More rows follow/);
    expect(listNotice(page({ count: 7, limit: 50 }))).toMatch(/last page of this selection/);
  });

  it('routes an empty page to the empty notice rather than to a row count', () => {
    expect(listNotice(page())).toBe(EMPTY_BASIS);
  });
});

describe('distribution groups', () => {
  it('keeps the server’s dimensions, including one this page does not know', () => {
    const rows = groupRows({
      run_id: 'run-1',
      groups: [group({ group: { operation: 'files.list', runtime: 'claude' } })],
    });
    expect(rows[0].dimensions).toEqual([
      { name: 'operation', value: 'files.list' },
      { name: 'runtime', value: 'claude' },
    ]);
  });

  it('says out loud when a dimension is absent, and when a path was never classified', () => {
    expect(dimensionValue('startup_path', null)).toBe('Not a startup');
    expect(dimensionValue('startup_path', 'unknown')).toBe('Unclassified');
    expect(dimensionValue('operation', null)).toBe('Not reported');
    expect(dimensionValue('boundary_version', 0)).toBe('0');
  });

  it('states what was stored, what was eligible, and what was excluded', () => {
    expect(groupBasis(group())).toBe(
      '30 stored sample(s), 12 interaction(s), 25 eligible for the percentiles; excluded: 3 with collection incomplete, 2 not successful. A sample can be both, so these may overlap.',
    );
    expect(groupBasis(group({ incomplete_count: 0, failed_count: 0 }))).toMatch(
      /nothing was excluded/,
    );
  });

  it('does not present the two exclusion counts as a partition of the stored rows', () => {
    // A sample whose collection never closed and whose outcome failed is in
    // both counts, so their sum can exceed what was actually excluded. The
    // sentence has to say that rather than invite the addition.
    const both = group({
      sample_count: 10,
      eligible_count: 4,
      incomplete_count: 4,
      failed_count: 4,
    });
    expect(groupBasis(both)).toContain('may overlap');
    expect(groupBasis(both)).not.toMatch(/\bexcluded: 8\b/);
  });

  it('flags a sample too small for its percentile to be read as a distribution', () => {
    expect(sampleCaution(MIN_PERCENTILE_SAMPLES - 1)).toMatch(/Fewer than 20 eligible samples/);
    expect(sampleCaution(MIN_PERCENTILE_SAMPLES)).toBeNull();
    expect(
      groupRows({ run_id: 'run-1', groups: [group({ eligible_count: 3 })] })[0].insufficient,
    ).toBe(true);
    expect(groupRows({ run_id: 'run-1', groups: [group()] })[0].insufficient).toBe(false);
  });

  it('renders a percentile with no eligible sample as unavailable, not as zero', () => {
    const rows = groupRows({
      run_id: 'run-1',
      groups: [
        group({ eligible_count: 0, p50_us: null, p95_us: null, min_us: null, max_us: null }),
      ],
    });
    expect(rows[0].p50.display).toBe('Unavailable');
    expect(rows[0].p95.display).toBe('Unavailable');
  });

  it('carries the sentence the distribution may not be read without', () => {
    expect(DISTRIBUTION_BASIS).toMatch(/whole experiment, not over the page/);
    expect(DISTRIBUTION_BASIS).toMatch(/not an end-to-end latency/);
  });

  it('counts stored and eligible samples across groups without inventing a total', () => {
    const response: MeasurementSummaryResponse = {
      run_id: 'run-1',
      groups: [group(), group({ sample_count: 5, eligible_count: 5, interaction_count: 2 })],
    };
    expect(summaryTotals(response)).toEqual({
      samples: 35,
      eligible: 30,
      incomplete: 6,
      failed: 4,
      // Distinct interactions are not additive across groups, so the largest
      // group's count is a floor rather than a fabricated sum.
      interactions: 12,
    });
  });
});

describe('selection and links', () => {
  it('reads a full selection out of a URL, repeats included', () => {
    const { selection, error } = selectionFromQuery(
      'run=run-1&operation=files.list&operation=files.open&coverage_scope=server_request' +
        '&outcome=completed&collection_complete=false&job_id=ajob_1&thread_id=athr_1' +
        '&attempt_id=42&interaction_id=aint_1&after=msm_9',
    );
    expect(error).toBeNull();
    expect(selection).toEqual({
      run: 'run-1',
      measurement: null,
      operation: ['files.list', 'files.open'],
      request_kind: [],
      coverage_scope: ['server_request'],
      outcome: 'completed',
      collection_complete: false,
      job_id: 'ajob_1',
      thread_id: 'athr_1',
      attempt_id: '42',
      interaction_id: 'aint_1',
      after: 'msm_9',
    });
  });

  it('refuses an attempt id that is not a number rather than widening to every attempt', () => {
    // Dropping it would list the whole experiment while the address bar still
    // said one attempt, which is the failure this page refuses everywhere else.
    expect(selectionFromQuery('attempt_id=latest').error).toMatch(
      /attempt_id=latest, which is not an attempt number/,
    );
    expect(selectionFromQuery('attempt_id=-1').error).toMatch(/not an attempt number/);
    expect(selectionFromQuery('attempt_id=1.5').error).toMatch(/not an attempt number/);
    // A well-formed one is carried as the text the URL holds.
    expect(selectionFromQuery('attempt_id=0').error).toBeNull();
    expect(selectionFromQuery('attempt_id=0').selection.attempt_id).toBe('0');
  });

  it('refuses a parameter it cannot express instead of widening the read', () => {
    expect(selectionFromQuery('run=run-1&host=host-a').error).toBe('Unsupported filter: host');
  });

  it('refuses a collection_complete that is neither true nor false', () => {
    expect(selectionFromQuery('collection_complete=yes').error).toMatch(
      /collection_complete=yes, which is not true or false/,
    );
  });

  it('treats an empty parameter as absent rather than as a name', () => {
    expect(selectionFromQuery('run=&after=').selection.run).toBeNull();
    expect(selectionFromQuery('run=&after=').selection.after).toBeNull();
    expect(selectionFromQuery('').selection).toEqual(EMPTY_SELECTION);
  });

  it('serializes repeats and drops absent values', () => {
    const href = selectionHref(MEASUREMENTS_PATH, {
      ...EMPTY_SELECTION,
      run: 'run one',
      operation: ['files.list', 'files.open'],
      collection_complete: false,
    });
    const params = new URLSearchParams(href.slice(href.indexOf('?') + 1));
    expect(href.startsWith(`${MEASUREMENTS_PATH}?`)).toBe(true);
    expect(params.get('run')).toBe('run one');
    expect(params.getAll('operation')).toEqual(['files.list', 'files.open']);
    expect(params.get('collection_complete')).toBe('false');
    expect(params.has('after')).toBe(false);
    expect(params.has('outcome')).toBe(false);
  });

  it('writes a bare path when nothing is selected', () => {
    expect(selectionHref(MEASUREMENTS_PATH, EMPTY_SELECTION)).toBe(MEASUREMENTS_PATH);
  });

  it('splits and de-duplicates a filter field', () => {
    expect(splitValues('files.list, files.open')).toEqual(['files.list', 'files.open']);
    expect(splitValues('a  a  b')).toEqual(['a', 'b']);
    expect(splitValues('  ')).toEqual([]);
  });

  it('takes an apply from the form, clearing what was cleared and dropping the cursor', () => {
    const values: Record<string, string> = {
      operation: 'files.list, files.open',
      request_kind: '',
      coverage_scope: 'server_request',
      outcome: ' failed ',
      collection_complete: 'false',
      job_id: '',
      thread_id: 'athr_1',
    };
    const selection = selectionFromForm((key) => values[key] ?? '', 'run-1');
    expect(selection).toMatchObject({
      run: 'run-1',
      operation: ['files.list', 'files.open'],
      request_kind: [],
      coverage_scope: ['server_request'],
      outcome: 'failed',
      collection_complete: false,
      job_id: null,
      thread_id: 'athr_1',
    });
    // The cursor is a position in one ordering, and the expanded record belongs
    // to the selection that listed it, so neither survives a new filter set.
    expect(selection.after).toBeNull();
    expect(selection.measurement).toBeNull();
  });

  it('summarizes the applied selection and says whether it narrows anything', () => {
    expect(selectionSummary(EMPTY_SELECTION)).toBe('Showing all experiments.');
    const filtered = selectionSummary({
      ...EMPTY_SELECTION,
      run: 'run-1',
      operation: ['files.list'],
      collection_complete: false,
    });
    expect(filtered).toMatch(/experiment run-1/);
    expect(filtered).toMatch(/operation in files.list/);
    expect(filtered).toMatch(/collection incomplete only/);
    expect(isFiltered(EMPTY_SELECTION)).toBe(false);
    expect(isFiltered({ ...EMPTY_SELECTION, run: 'run-1' })).toBe(false);
    expect(isFiltered({ ...EMPTY_SELECTION, outcome: 'failed' })).toBe(true);
  });
});

describe('record detail helpers', () => {
  it('renders facts without flattening a nested value into [object Object]', () => {
    expect(factLines({ b: 1, a: 'x' }).map((line) => line.name)).toEqual(['a', 'b']);
    expect(factValue({ a: 1 })).toBe('{"a":1}');
    expect(factValue(null)).toBe('Not reported');
    expect(factValue(true)).toBe('true');
    expect(factLines(undefined)).toEqual([]);
  });

  it('renders run targets, keeping an empty selector visible as unreported', () => {
    expect(targetLines({ host: 'host-a', job_id: '' })).toEqual([
      { name: 'host', value: 'host-a' },
      { name: 'job_id', value: 'Not reported' },
    ]);
    expect(targetLines(undefined)).toEqual([]);
  });

  it('names a detail by operation and request kind only when they differ', () => {
    expect(detailHeading({ operation: 'files.list', request_kind: 'files.list' })).toBe(
      'files.list',
    );
    expect(detailHeading({ operation: 'terminal.open', request_kind: 'terminal.create' })).toBe(
      'terminal.open · terminal.create',
    );
  });
});
