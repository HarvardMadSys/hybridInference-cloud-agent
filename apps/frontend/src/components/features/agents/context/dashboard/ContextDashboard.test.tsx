// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { LocaleProvider } from '@/components/providers/LocaleProvider';
import {
  getRequestComposition,
  getThreadContextDna,
  getThreadContextTiming,
  type ContextPointApi,
  type RequestCompositionApi,
  type ThreadContextApi,
  type ThreadTimingApi,
} from '@/lib/api/agents';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { ContextDashboard } from './ContextDashboard';

vi.mock('@/lib/api/agents', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/lib/api/agents')>()),
  getRequestComposition: vi.fn(),
  getThreadContextTiming: vi.fn(),
  getThreadContextDna: vi.fn(),
}));

const KEYS = ['user', 'tool_results'];

function point(index: number, overrides: Partial<ContextPointApi> = {}): ContextPointApi {
  return {
    index,
    job_id: 'j',
    turn_no: index < 2 ? 1 : 2,
    attempt_id: index < 2 ? 1 : 2,
    seq: index + 1,
    event_id: 100 + index,
    at: null,
    prompt_tokens: 1000 * (index + 1),
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    uncached_input_tokens: 0,
    output_tokens: 10,
    context_window: index === 3 ? 200_000 : null,
    model: null,
    delta_tokens: null,
    after_compaction: false,
    categories: [100 * (index + 1), 200 * (index + 1)],
    step_ms: 1000,
    usage_id: `r${index}`,
    ...overrides,
  };
}

function view(): ThreadContextApi {
  const requests = [0, 1, 2, 3].map((i) => point(i));
  return {
    thread_id: 't',
    generated_at: '2026-10-29T12:00:00Z',
    capabilities: [],
    latest: { point: requests[3], window_share: 0.02, compacted_after: false, turn_active: false },
    peak: requests[3],
    turns: [],
    requests,
    compactions: [],
    largest_changes: [],
    coverage: {
      turns: 2,
      attempts: 2,
      attempts_observed: 2,
      main_requests: 4,
      subagent_requests: 0,
      compactions: 0,
      compactions_unknown_outcome: 0,
      points_omitted: 0,
      unparsed_raw: 0,
    },
    events: [],
    event_counts: {},
    events_omitted: 0,
    event_capabilities: [],
    category_keys: KEYS,
    tokens: {
      basis: 'chars_cjk_v1',
      recorded: [
        { key: 'user', tokens: 400, items: 2 },
        { key: 'tool_results', tokens: 800, items: 3 },
      ],
      recorded_total: 1200,
      not_recorded: ['system_prompt', 'tool_schemas'],
      turns_read: 2,
      turns: 2,
      truncated: false,
      provider: {
        requests: 4,
        input_tokens: 10_000,
        cache_read_tokens: 6_000,
        cache_write_tokens: 1_000,
        output_tokens: 40,
        output_unreported: 0,
        subagent_requests: 0,
        subagent_input_tokens: 0,
        subagent_output_tokens: 0,
        cache_hit: 0.6,
      },
      reported_by: 'provider',
    },
  };
}

const TIMING: ThreadTimingApi = {
  active_ms: 10_000,
  totals: {
    ttft: 1000,
    reasoning: 0,
    text: 0,
    toolarg: 0,
    gen: 3000,
    tools: 4000,
    unsplit: 0,
    other: 2000,
  },
  executions: 2,
  executions_split: 2,
  model_requests: 4,
  tool_calls: 3,
  tool_calls_unpaired: 1,
  blocks: {},
  output_tps: 13.3,
  gateway: 'available',
  gateway_reason: null,
  segments: [
    { kind: 'ttft', start_ms: 0, end_ms: 1000 },
    { kind: 'gen', start_ms: 1000, end_ms: 4000 },
    { kind: 'tools', start_ms: 4000, end_ms: 8000 },
    { kind: 'other', start_ms: 8000, end_ms: 10_000 },
  ],
  segments_merged: false,
  requests: [{ response_id: 'r3', ttft_ms: 200, latency_ms: 900 }],
};

function composition(eventId: number): RequestCompositionApi {
  const request = view().requests.find((p) => p.event_id === eventId)!;
  const n = request.index + 1;
  return {
    thread_id: 't',
    request,
    runtime: 'codex',
    native: null,
    native_relation: null,
    native_status: 'not_offered',
    reconstructed: {
      basis: 'stored_events_transcript_order',
      token_basis: 'chars_cjk_v1',
      categories: [
        { key: 'user_messages', entries: 1, chars: 8, tokens: 6, truncated_entries: 0 },
        { key: 'tool_results', entries: n, chars: 400 * n, tokens: 100 * n, truncated_entries: 0 },
      ],
      entries: [
        {
          category: 'user_messages',
          label: 'prompt',
          event_id: null,
          job_id: 'j',
          turn_no: 1,
          chars: 8,
          tokens: 6,
          preview: 'hello',
          truncated: false,
        },
        ...Array.from({ length: n }, (_, i) => ({
          category: 'tool_results',
          label: `Read ${i}`,
          event_id: 900 + i,
          job_id: 'j',
          turn_no: 1,
          chars: 400,
          tokens: 100,
          preview: `result ${i}`,
          truncated: false,
        })),
      ],
      entries_total: n + 1,
      entries_omitted: 0,
      estimated_tokens: 6 + 100 * n,
      boundary: null,
      excluded: {
        compacted: { entries: 0, chars: 0 },
        superseded_attempts: { entries: 0, chars: 0 },
        subagent: { entries: 0, chars: 0 },
        this_request_output: { entries: 0, chars: 0 },
      },
      copied_turns: 0,
      events_capped: false,
    },
  };
}

function Harness({
  jobId = 'j',
  conversationKey = 'k',
  data = view(),
}: {
  jobId?: string;
  conversationKey?: string;
  data?: ThreadContextApi;
}) {
  // The selection is held as ContextView holds it.
  const [selected, setSelected] = useState<number | null>(null);
  return (
    <ContextDashboard
      jobId={jobId}
      conversationKey={conversationKey}
      view={data}
      nowMs={Date.parse('2026-10-29T12:00:00Z')}
      selected={selected}
      onSelect={setSelected}
      onOpenPoint={vi.fn()}
      onOpenTurn={vi.fn()}
    />
  );
}

function show(props: Parameters<typeof Harness>[0] = {}) {
  return render(
    <LocaleProvider>
      <Harness {...props} />
    </LocaleProvider>,
  );
}

function card(title: string): HTMLElement {
  return screen.getByRole('heading', { name: title }).closest('section')!;
}

beforeEach(() => {
  vi.mocked(getThreadContextTiming).mockResolvedValue(TIMING);
  vi.mocked(getThreadContextDna).mockImplementation(async (_job, from, to) => ({
    basis: 'chars_cjk_v1',
    requests: Array.from({ length: to - from + 1 }, (_, i) => ({
      index: from + i,
      event_id: 100 + from + i,
      items: [
        ['p1', 'user', 100],
        ...Array.from(
          { length: from + i + 1 },
          (_, k) => [`e${k}`, 'tool_results', 200] as [string, string, number],
        ),
      ],
      folded_items: 0,
      folded_tokens: 0,
    })),
    events_capped: false,
  }));
  vi.mocked(getRequestComposition).mockImplementation(async (_job, eventId) =>
    composition(eventId),
  );
});

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
  window.localStorage.clear();
});

describe('ContextDashboard', () => {
  it('reads timing once, and shows its measured parts with TPS', async () => {
    show();
    const timing = card('Timing Stats');
    expect(await within(timing).findByText('13 tok/s')).toBeInTheDocument();
    expect(within(timing).getByText('Not attributed')).toBeInTheDocument();
    expect(getThreadContextTiming).toHaveBeenCalledTimes(1);
  });

  it('drops a timing reply for the conversation it no longer shows', async () => {
    let resolveFirst: (value: ThreadTimingApi) => void = () => undefined;
    vi.mocked(getThreadContextTiming)
      .mockImplementationOnce(() => new Promise((resolve) => (resolveFirst = resolve)))
      .mockResolvedValueOnce(null);
    const { rerender } = show({ jobId: 'a', conversationKey: 'a' });
    rerender(
      <LocaleProvider>
        <Harness jobId="b" conversationKey="b" />
      </LocaleProvider>,
    );
    await act(async () => resolveFirst(TIMING));
    expect(screen.queryByText('13 tok/s')).not.toBeInTheDocument();
  });

  it('switches the token basis between recorded estimates and reported usage', async () => {
    show();
    const tokens = card('Token Stats');
    fireEvent.click(within(tokens).getByRole('button', { name: 'Reported' }));
    expect(within(tokens).getByRole('button', { name: 'Reported' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(within(tokens).getAllByText('6,000').length).toBeGreaterThan(0);
  });

  it('aggregates by turn, and offers DNA only per step', async () => {
    show();
    const trend = card('Context Trend');
    expect(within(trend).getByRole('group', { name: /Context of 4 columns/ })).toBeInTheDocument();
    fireEvent.click(within(trend).getByRole('button', { name: 'Turn' }));
    expect(within(trend).getByRole('group', { name: /Context of 2 columns/ })).toBeInTheDocument();
    expect(within(trend).getByRole('button', { name: 'DNA' })).toBeDisabled();
  });

  it('reads DNA for the visible requests only when it is shown', async () => {
    show();
    const trend = card('Context Trend');
    expect(getThreadContextDna).not.toHaveBeenCalled();
    fireEvent.click(within(trend).getByRole('button', { name: 'DNA' }));
    await vi.waitFor(() =>
      expect(getThreadContextDna).toHaveBeenCalledWith('j', 0, 3, expect.anything()),
    );
    expect(getThreadContextDna).toHaveBeenCalledTimes(1);
  });

  it('offers duration once a request joins a measured latency', async () => {
    show();
    const trend = card('Context Trend');
    const duration = within(trend).getByRole('button', { name: 'Duration' });
    await vi.waitFor(() => expect(duration).toBeEnabled());
    fireEvent.click(duration);
    expect(duration).toHaveAttribute('aria-pressed', 'true');
  });

  it('moves the selection with the keyboard, and the browser follows it', async () => {
    show();
    const browser = card('Context Browser');
    expect(await within(browser).findByText('Request 4 · turn 2')).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('group', { name: /Context of 4 columns/ }), {
      key: 'ArrowLeft',
    });
    expect(await within(browser).findByText('Request 3 · turn 2')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Selected request' })).toBeInTheDocument();
    fireEvent.click(within(browser).getByRole('button', { name: 'Previous request' }));
    expect(await within(browser).findByText('Request 2 · turn 1')).toBeInTheDocument();
  });

  it('marks items new since the compared request, by identity', async () => {
    show();
    const browser = card('Context Browser');
    const row = await within(browser).findByRole('button', { name: /Tool results/ });
    await vi.waitFor(() => expect(row).toHaveTextContent('+1'));
    fireEvent.click(row);
    expect(within(browser).getByText('1 new since the compared request')).toBeInTheDocument();
    expect(within(browser).getAllByText('new')).toHaveLength(1);
    fireEvent.change(within(browser).getByRole('searchbox'), { target: { value: 'nothing' } });
    expect(within(browser).getByText('No item in this category matches.')).toBeInTheDocument();
  });

  it('drops a composition reply for a request no longer selected', async () => {
    const pending = new Map<number, (value: RequestCompositionApi) => void>();
    vi.mocked(getRequestComposition).mockImplementation(
      (_job, eventId) => new Promise((resolve) => pending.set(eventId, resolve)),
    );
    show();
    const browser = card('Context Browser');
    fireEvent.keyDown(screen.getByRole('group', { name: /Context of 4 columns/ }), {
      key: 'ArrowLeft',
    });
    await vi.waitFor(() => expect(pending.has(102)).toBe(true));
    await act(async () => pending.get(103)?.(composition(103)));
    // Request 4's reply arrives after request 3 was chosen: not shown.
    expect(within(browser).queryByText(/Observed 4,000/)).not.toBeInTheDocument();
    await act(async () => pending.get(102)?.(composition(102)));
    expect(within(browser).queryByText(/Observed 3,000/)).toBeInTheDocument();
  });

  it('shows the window share only for a reported window', async () => {
    show();
    const latest = card('Latest observed request');
    expect(within(latest).getByText('2.0%')).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole('group', { name: /Context of 4 columns/ }), {
      key: 'ArrowLeft',
    });
    const selected = card('Selected request');
    expect(within(selected).queryByText(/^\d+\.\d%$/)).toBeNull();
    expect(
      within(selected).getByText('The window size was not reported, so no share is shown.'),
    ).toBeInTheDocument();
  });

  it('keeps the selected request in a strip of the trend card, following the selection', async () => {
    show();
    const trend = card('Context Trend');
    const strip = within(trend).getByRole('group', { name: 'Request details' });
    expect(within(strip).getByText('Request 4 · turn 2 · 4,000')).toBeInTheDocument();
    expect(within(strip).getByText(/0 written · 0 uncached/)).toHaveAttribute(
      'title',
      expect.stringMatching(/billing, not more content/),
    );
    // The browser's own navigation moves the trend's strip too.
    const browser = card('Context Browser');
    fireEvent.click(within(browser).getByRole('button', { name: 'Previous request' }));
    expect(within(strip).getByText('Request 3 · turn 2 · 3,000')).toBeInTheDocument();
    expect(await within(browser).findByText('Request 3 · turn 2')).toBeInTheDocument();
    // No second full-width detail panel.
    expect(screen.getAllByRole('group', { name: 'Request details' })).toHaveLength(1);
  });

  it('brings a request selected outside the trend window into view', async () => {
    const data = view();
    data.requests = Array.from({ length: 130 }, (_, i) =>
      point(i, { turn_no: 1 + Math.floor(i / 10), event_id: 100 + i, usage_id: null }),
    );
    data.latest = { ...data.latest!, point: data.requests[129] };
    function Jump() {
      const [selected, setSelected] = useState<number | null>(null);
      return (
        <>
          <button type="button" onClick={() => setSelected(2)}>
            jump
          </button>
          <ContextDashboard
            jobId="j"
            conversationKey="k"
            view={data}
            nowMs={0}
            selected={selected}
            onSelect={setSelected}
            onOpenPoint={vi.fn()}
            onOpenTurn={vi.fn()}
          />
        </>
      );
    }
    render(
      <LocaleProvider>
        <Jump />
      </LocaleProvider>,
    );
    const trend = card('Context Trend');
    expect(within(trend).getByText('Showing 11–130 of 130')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'jump' }));
    expect(within(trend).getByText('Showing 3–122 of 130')).toBeInTheDocument();
    expect(within(trend).getByText('Request 3 · turn 1 · 3,000')).toBeInTheDocument();
  });

  it('measures its own width: two columns, the top pair, or one column', () => {
    const widths = [900, 700, 390];
    const observers: Array<() => void> = [];
    class FakeObserver {
      constructor(private readonly callback: ResizeObserverCallback) {}
      observe() {
        observers.push(() =>
          this.callback(
            [{ contentRect: { width: widths[0] } } as ResizeObserverEntry],
            this as unknown as ResizeObserver,
          ),
        );
      }
      disconnect() {}
      unobserve() {}
    }
    vi.stubGlobal('ResizeObserver', FakeObserver);
    try {
      for (const [width, layout] of [
        [900, 'wide'],
        [700, 'medium'],
        [390, 'narrow'],
      ] as const) {
        widths[0] = width;
        observers.length = 0;
        const { container, unmount } = show();
        act(() => observers.forEach((fire) => fire()));
        const root = container.querySelector('[data-layout]')!;
        expect(root).toHaveAttribute('data-layout', layout);
        const browser = card('Context Browser');
        const trend = card('Context Trend');
        // Wide: the browser beside the window and trend; otherwise below them.
        if (layout === 'wide') {
          expect(browser.parentElement).not.toBe(root);
          expect(browser.parentElement).toContainElement(trend);
          expect(browser.parentElement?.className).toMatch(/grid-cols-/);
        } else {
          expect(browser.parentElement).toBe(root);
        }
        expect(trend.compareDocumentPosition(browser)).toBe(Node.DOCUMENT_POSITION_FOLLOWING);
        unmount();
      }
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('keeps the exact request while the chart highlights its turn column', async () => {
    const data = view();
    // 130 turns of two requests each, so the default window (last 120) hides
    // the early turns.
    data.requests = Array.from({ length: 260 }, (_, i) =>
      point(i, {
        turn_no: 1 + Math.floor(i / 2),
        job_id: `t${1 + Math.floor(i / 2)}`,
        attempt_id: 1 + Math.floor(i / 2),
        event_id: 100 + i,
        usage_id: null,
      }),
    );
    data.latest = { ...data.latest!, point: data.requests[259] };
    function Jump() {
      const [selected, setSelected] = useState<number | null>(null);
      return (
        <>
          <button type="button" onClick={() => setSelected(4)}>
            jump
          </button>
          <ContextDashboard
            jobId="j"
            conversationKey="k"
            view={data}
            nowMs={0}
            selected={selected}
            onSelect={setSelected}
            onOpenPoint={vi.fn()}
            onOpenTurn={vi.fn()}
          />
        </>
      );
    }
    render(
      <LocaleProvider>
        <Jump />
      </LocaleProvider>,
    );
    const trend = card('Context Trend');
    fireEvent.click(within(trend).getByRole('button', { name: 'Turn' }));
    expect(within(trend).getByText('Showing 11–130 of 130')).toBeInTheDocument();
    // Request 5 is turn 3's first request, not the turn's last (request 6).
    fireEvent.click(screen.getByRole('button', { name: 'jump' }));
    expect(within(trend).getByText('Showing 3–122 of 130')).toBeInTheDocument();
    expect(within(trend).getByText(/Turn 3 · 2 requests/)).toBeInTheDocument();
    const strip = within(trend).getByRole('group', { name: 'Request details' });
    expect(within(strip).getByText('Request 5 · turn 3 · 5,000')).toBeInTheDocument();
    // Arrow keys move turn columns, and select that turn's own last request.
    fireEvent.keyDown(within(trend).getByRole('group', { name: /Context of 130 columns/ }), {
      key: 'ArrowRight',
    });
    expect(within(trend).getByText(/Turn 4 · 2 requests/)).toBeInTheDocument();
    expect(within(strip).getByText('Request 8 · turn 4 · 8,000')).toBeInTheDocument();
    // The browser shows the same exact request.
    expect(
      await within(card('Context Browser')).findByText('Request 8 · turn 4'),
    ).toBeInTheDocument();
  });

  it('navigates by request in Turn mode, and the turnaround follows its turn', async () => {
    show();
    const trend = card('Context Trend');
    fireEvent.click(within(trend).getByRole('button', { name: 'Turn' }));
    const strip = within(trend).getByRole('group', { name: 'Request details' });
    const browser = card('Context Browser');
    // Turn 2's column stands for request 4 (its last); going back one request
    // keeps the chart on turn 2 but shows request 3 exactly.
    fireEvent.click(within(browser).getByRole('button', { name: 'Previous request' }));
    expect(within(strip).getByText('Request 3 · turn 2 · 3,000')).toBeInTheDocument();
    expect(within(trend).getByText(/Turn 2 · 2 requests/)).toBeInTheDocument();
    fireEvent.click(within(browser).getByRole('button', { name: 'Previous request' }));
    expect(within(strip).getByText('Request 2 · turn 1 · 2,000')).toBeInTheDocument();
    expect(within(trend).getByText(/Turn 1 · 2 requests/)).toBeInTheDocument();
    // Nothing to move along on an empty chart: no selection is made.
    cleanup();
    show({ data: { ...view(), requests: [], latest: null, peak: null } });
    const empty = card('Context Trend');
    expect(within(empty).getByRole('group', { name: /Context of 0 columns/ })).toBeInTheDocument();
    expect(within(empty).getByRole('button', { name: 'Overview' })).toBeDisabled();
    fireEvent.keyDown(within(empty).getByRole('group', { name: /Context of 0 columns/ }), {
      key: 'ArrowLeft',
    });
    expect(screen.queryByRole('group', { name: 'Request details' })).not.toBeInTheDocument();
  });

  it('replaces the browsing window with the whole conversation, and puts the window back', () => {
    const data = view();
    data.requests = Array.from({ length: 130 }, (_, i) =>
      point(i, {
        turn_no: 1 + Math.floor(i / 10),
        event_id: 100 + i,
        usage_id: null,
      }),
    );
    data.latest = { ...data.latest!, point: data.requests[129] };
    show({ data });
    const trend = card('Context Trend');
    // The old miniature navigator and its buttons are gone before Overview is used.
    expect(within(trend).queryByRole('button', { name: 'Earlier' })).not.toBeInTheDocument();
    expect(within(trend).queryByRole('button', { name: 'Later' })).not.toBeInTheDocument();
    expect(within(trend).queryByRole('button', { name: 'All' })).not.toBeInTheDocument();
    expect(within(trend).getByText('Showing 11–130 of 130')).toBeInTheDocument();
    const overview = within(trend).getByRole('button', { name: 'Overview' });
    expect(overview).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(overview);
    expect(overview).toHaveAttribute('aria-pressed', 'true');
    expect(within(trend).getByText('Showing 1–130 of 130')).toBeInTheDocument();
    // The whole conversation is drawn, but the request count is unchanged: no read.
    expect(
      within(trend).getByRole('group', { name: /Context of 130 columns/ }),
    ).toBeInTheDocument();
    expect(within(trend).getByRole('button', { name: 'DNA' })).toBeDisabled();
    expect(within(trend).getByRole('button', { name: 'Adaptive' })).toBeDisabled();
    // Off restores the range that was being browsed, not the latest one.
    fireEvent.click(overview);
    expect(overview).toHaveAttribute('aria-pressed', 'false');
    expect(within(trend).getByText('Showing 11–130 of 130')).toBeInTheDocument();
  });

  it('leaves DNA for Overview without reading more items, and keeps the range', async () => {
    show();
    const trend = card('Context Trend');
    fireEvent.click(within(trend).getByRole('button', { name: 'DNA' }));
    await vi.waitFor(() =>
      expect(getThreadContextDna).toHaveBeenCalledWith('j', 0, 3, expect.anything()),
    );
    expect(getThreadContextDna).toHaveBeenCalledTimes(1);
    fireEvent.click(within(trend).getByRole('button', { name: 'Overview' }));
    expect(within(trend).getByRole('button', { name: 'DNA' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
    expect(within(trend).getByRole('button', { name: 'DNA' })).toBeDisabled();
    // Toggling the view read no more items.
    await act(async () => undefined);
    expect(getThreadContextDna).toHaveBeenCalledTimes(1);
    fireEvent.click(within(trend).getByRole('button', { name: 'Overview' }));
    expect(within(trend).getByText('Showing 1–4 of 4')).toBeInTheDocument();
  });

  it('keeps the saved range when requests append while Overview is on', () => {
    const first = view();
    first.requests = Array.from({ length: 130 }, (_, i) =>
      point(i, { turn_no: 1 + Math.floor(i / 10), event_id: 100 + i, usage_id: null }),
    );
    first.latest = { ...first.latest!, point: first.requests[129] };
    const grown = view();
    grown.requests = Array.from({ length: 135 }, (_, i) =>
      point(i, { turn_no: 1 + Math.floor(i / 10), event_id: 100 + i, usage_id: null }),
    );
    grown.latest = { ...grown.latest!, point: grown.requests[134] };
    const { rerender } = render(
      <LocaleProvider>
        <Harness data={first} />
      </LocaleProvider>,
    );
    const trend = card('Context Trend');
    fireEvent.click(within(trend).getByRole('button', { name: 'Overview' }));
    expect(within(trend).getByText('Showing 1–130 of 130')).toBeInTheDocument();
    rerender(
      <LocaleProvider>
        <Harness data={grown} />
      </LocaleProvider>,
    );
    expect(within(trend).getByText('Showing 1–135 of 135')).toBeInTheDocument();
    fireEvent.click(within(trend).getByRole('button', { name: 'Overview' }));
    // The saved browsing range stays where it was; the new requests stay outside it.
    expect(within(trend).getByText('Showing 11–130 of 135')).toBeInTheDocument();
  });

  it('leaves Overview when the conversation changes', () => {
    const { rerender } = show();
    const trend = card('Context Trend');
    fireEvent.click(within(trend).getByRole('button', { name: 'Overview' }));
    expect(within(trend).getByRole('button', { name: 'Overview' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    rerender(
      <LocaleProvider>
        <Harness conversationKey="other" />
      </LocaleProvider>,
    );
    expect(within(card('Context Trend')).getByRole('button', { name: 'Overview' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );
  });

  it('restores the browsing turns after a granularity change during Overview', () => {
    const data = view();
    // 130 turns of two requests: the default step window is 141–260, turns 71–130.
    data.requests = Array.from({ length: 260 }, (_, i) =>
      point(i, {
        turn_no: 1 + Math.floor(i / 2),
        event_id: 100 + i,
        usage_id: null,
      }),
    );
    data.latest = { ...data.latest!, point: data.requests[259] };
    show({ data });
    const trend = card('Context Trend');
    expect(within(trend).getByText('Showing 141–260 of 260')).toBeInTheDocument();
    fireEvent.click(within(trend).getByRole('button', { name: 'Overview' }));
    fireEvent.click(within(trend).getByRole('button', { name: 'Turn' }));
    expect(within(trend).getByText('Showing 1–130 of 130')).toBeInTheDocument();
    fireEvent.click(within(trend).getByRole('button', { name: 'Overview' }));
    expect(within(trend).getByText('Showing 71–130 of 130')).toBeInTheDocument();
  });

  it('restores the range around a request selected outside the trend while Overview is on', async () => {
    const data = view();
    data.requests = Array.from({ length: 130 }, (_, i) =>
      point(i, { turn_no: 1 + Math.floor(i / 10), event_id: 100 + i, usage_id: null }),
    );
    data.latest = { ...data.latest!, point: data.requests[129] };
    function Jump() {
      const [selected, setSelected] = useState<number | null>(null);
      return (
        <>
          <button type="button" onClick={() => setSelected(2)}>
            jump
          </button>
          <ContextDashboard
            jobId="j"
            conversationKey="k"
            view={data}
            nowMs={0}
            selected={selected}
            onSelect={setSelected}
            onOpenPoint={vi.fn()}
            onOpenTurn={vi.fn()}
          />
        </>
      );
    }
    render(
      <LocaleProvider>
        <Jump />
      </LocaleProvider>,
    );
    const trend = card('Context Trend');
    fireEvent.click(within(trend).getByRole('button', { name: 'Overview' }));
    fireEvent.click(screen.getByRole('button', { name: 'jump' }));
    // The whole conversation still shows; the saved range now contains request 3.
    expect(within(trend).getByText('Showing 1–130 of 130')).toBeInTheDocument();
    expect(within(trend).getByText('Request 3 · turn 1 · 3,000')).toBeInTheDocument();
    fireEvent.click(within(trend).getByRole('button', { name: 'Overview' }));
    expect(within(trend).getByText('Showing 3–122 of 130')).toBeInTheDocument();
    expect(
      await within(card('Context Browser')).findByText('Request 3 · turn 1'),
    ).toBeInTheDocument();
  });

  it('answers in Chinese', async () => {
    window.localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    show();
    expect(await screen.findByRole('heading', { name: '上下文趋势' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '上下文浏览器' })).toBeInTheDocument();
  });
});
