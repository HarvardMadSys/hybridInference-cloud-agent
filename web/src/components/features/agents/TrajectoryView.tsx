'use client';

// The trajectory: every stored event of the conversation, in order, with the payload
// behind each one.
//
// The chat tab beside this is a reading of the stream. This is the stream —
// unfolded, undeduplicated, including the rows no view knows how to render.
// It exists for the question the chat view cannot answer: not "what did the
// agent say" but "what actually happened, and how long did each step take".

import { useMemo, useState } from 'react';

import type { AgentJobEventApi, AgentThreadEventApi } from '@/lib/api/agents';
import { formatDuration } from './TurnFooter';
import {
  filterTrajectory,
  toSessionLog,
  toTrajectory,
  type TrajectoryLane,
  type TrajectoryRow,
} from './trajectory';

const LANES: { key: TrajectoryLane; label: string; tone: string }[] = [
  { key: 'input', label: 'Input', tone: 'bg-sky-400' },
  { key: 'model', label: 'Model', tone: 'bg-violet-400' },
  { key: 'tools', label: 'Tools', tone: 'bg-amber-400' },
  { key: 'other', label: 'Other', tone: 'bg-gray-300 dark:bg-gray-600' },
];

const ACTOR_TONE: Record<TrajectoryRow['actor'], string> = {
  user: 'bg-sky-50 dark:bg-sky-500/10 text-sky-700 dark:text-sky-400',
  assistant: 'bg-violet-50 dark:bg-violet-500/10 text-violet-700 dark:text-violet-400',
  tool: 'bg-amber-50 dark:bg-amber-500/10 text-amber-700 dark:text-amber-400',
  context: 'bg-gray-100 dark:bg-white/5 text-gray-600 dark:text-gray-400',
  system: 'bg-gray-100 dark:bg-white/5 text-gray-600 dark:text-gray-400',
};

/** A strip of one mark per event, so a long turn has a shape you can scan. */
function Minimap({
  rows,
  selectedId,
  onSelect,
}: {
  rows: TrajectoryRow[];
  selectedId: number | null;
  onSelect: (id: number) => void;
}) {
  const lanes = LANES.map((lane) => ({
    ...lane,
    count: rows.filter((row) => row.lane === lane.key).length,
  })).filter((lane) => lane.count > 0);

  return (
    <div className="flex gap-4">
      <div className="w-24 shrink-0 space-y-1.5">
        {lanes.map((lane) => (
          <div key={lane.key} className="flex h-5 items-center gap-2">
            <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full ${lane.tone}`} />
            <span className="text-[11px] font-medium text-gray-600 dark:text-gray-300">
              {lane.label}
            </span>
            <span className="ml-auto text-[10px] tabular-nums text-gray-400 dark:text-gray-500">
              {lane.count}
            </span>
          </div>
        ))}
      </div>
      <div className="min-w-0 flex-1 overflow-x-auto pb-1">
        <div className="space-y-1.5" style={{ minWidth: rows.length * 12 + 8 }}>
          {lanes.map((lane) => (
            <div
              key={lane.key}
              className="grid h-5 items-center gap-0.5 rounded-md bg-gray-50 px-1 dark:bg-white/[0.03]"
              style={{ gridTemplateColumns: `repeat(${rows.length}, minmax(0, 1fr))` }}
            >
              {rows.map((row) =>
                row.lane === lane.key ? (
                  <button
                    key={row.id}
                    type="button"
                    aria-label={`${row.label} (${row.eventType})`}
                    aria-pressed={selectedId === row.id}
                    title={`${row.label} (${row.eventType})${row.durationMs !== undefined ? ` · ${formatDuration(row.durationMs)}` : ''}`}
                    onClick={() => onSelect(row.id)}
                    className={`mx-auto h-3.5 w-full max-w-4 rounded-[3px] transition-opacity hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-gray-700 focus-visible:ring-offset-2 dark:focus-visible:ring-gray-200 dark:focus-visible:ring-offset-gray-900 ${
                      row.isError ? 'bg-rose-400' : lane.tone
                    } ${selectedId === row.id ? 'opacity-100 ring-1 ring-gray-700 ring-offset-1 dark:ring-gray-200 dark:ring-offset-gray-900' : selectedId === null ? 'opacity-80' : 'opacity-60'}`}
                  />
                ) : (
                  <span key={row.id} aria-hidden="true" />
                ),
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

/** What one event carried, shown only when a reader asks for it. */
function RowDetail({ row, resultRow }: { row: TrajectoryRow; resultRow?: TrajectoryRow }) {
  return (
    <div className="space-y-3 border-t border-gray-200 dark:border-white/10 bg-white dark:bg-gray-900 px-3 py-3 text-[12px]">
      <dl className="grid grid-cols-[6rem_1fr] gap-x-3 gap-y-1">
        <dt className="text-gray-500 dark:text-gray-400">Event</dt>
        <dd className="font-mono text-gray-800 dark:text-gray-200">
          {row.eventType} · seq {row.seq} · attempt {row.attemptId}
        </dd>
        <dt className="text-gray-500 dark:text-gray-400">Time</dt>
        <dd className="text-gray-800 dark:text-gray-200">{row.at ?? 'not recorded'}</dd>
        {row.durationMs !== undefined ? (
          <>
            <dt className="text-gray-500 dark:text-gray-400">Duration</dt>
            <dd className="text-gray-800 dark:text-gray-200">
              {formatDuration(row.durationMs)}{' '}
              <span className="text-gray-500 dark:text-gray-400">
                — from the request to its result, so it includes the model&rsquo;s own thinking
              </span>
            </dd>
          </>
        ) : null}
      </dl>
      <div>
        <p className="mb-1 font-medium text-gray-600 dark:text-gray-400">Payload</p>
        <pre className="max-h-64 overflow-auto rounded bg-gray-50 dark:bg-gray-950 p-2 font-mono text-[11px] leading-relaxed text-gray-800 dark:text-gray-200">
          {JSON.stringify(row.payload, null, 2)}
        </pre>
      </div>
      {resultRow ? (
        <div>
          <p className="mb-1 font-medium text-gray-600 dark:text-gray-400">Result</p>
          <pre className="max-h-64 overflow-auto rounded bg-gray-50 dark:bg-gray-950 p-2 font-mono text-[11px] leading-relaxed text-gray-800 dark:text-gray-200">
            {JSON.stringify(resultRow.payload, null, 2)}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

export function TrajectoryView({ events }: { events: (AgentJobEventApi | AgentThreadEventApi)[] }) {
  const [query, setQuery] = useState('');
  const [selectedId, setSelectedId] = useState<number | null>(null);

  const rows = useMemo(() => toTrajectory(events), [events]);
  const visible = useMemo(() => filterTrajectory(rows, query), [rows, query]);
  const byId = useMemo(() => new Map(rows.map((row) => [row.id, row])), [rows]);

  function download() {
    const blob = new Blob([toSessionLog(events)], { type: 'application/x-ndjson' });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = 'session-log.ndjson';
    link.click();
    URL.revokeObjectURL(url);
  }

  if (rows.length === 0) {
    return (
      <p className="py-8 text-center text-[13px] text-gray-500 dark:text-gray-400">
        This conversation stored no events. A thread that ran before event capture, or one whose
        first turn never started, has nothing to show here.
      </p>
    );
  }

  return (
    <section aria-label="Trajectory" className="space-y-3">
      <div className="rounded-xl border border-gray-200 bg-white shadow-[0_1px_2px_rgba(17,24,39,0.025)] dark:border-white/10 dark:bg-gray-900">
        <div className="flex items-center justify-between gap-3 border-b border-gray-100 px-4 py-3 dark:border-white/5">
          <h2 className="flex items-center gap-2 text-[12px] font-medium text-gray-700 dark:text-gray-200">
            <svg
              aria-hidden="true"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.25"
              strokeLinecap="round"
              className="h-3.5 w-3.5 text-gray-400 dark:text-gray-500"
            >
              <path d="M2.5 2.5v11h11M5.5 5h3M9.5 8h3M5.5 11h2" />
            </svg>
            Execution overview
          </h2>
          <span className="rounded-md bg-gray-50 px-2 py-0.5 text-[10px] tabular-nums text-gray-500 dark:bg-white/5 dark:text-gray-400">
            {rows.length} {rows.length === 1 ? 'event' : 'events'}
          </span>
        </div>
        <div className="px-4 pb-3 pt-3.5">
          <Minimap rows={rows} selectedId={selectedId} onSelect={setSelectedId} />
        </div>
      </div>

      <div className="flex items-center gap-2">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search events"
          aria-label="Search events"
          className="min-w-0 flex-1 rounded-lg border border-gray-200 dark:border-white/10 px-2.5 py-1.5 text-[13px] outline-none focus:border-gray-300"
        />
        {visible.length !== rows.length ? (
          <span className="shrink-0 text-[12px] tabular-nums text-gray-500 dark:text-gray-400">
            {visible.length} of {rows.length}
          </span>
        ) : null}
        <button
          type="button"
          onClick={download}
          className="shrink-0 rounded-lg border border-gray-200 dark:border-white/10 px-2.5 py-1.5 text-[12px] text-gray-600 dark:text-gray-400 transition-colors hover:border-gray-300 dark:hover:border-white/20 hover:text-gray-900 dark:hover:text-gray-100"
        >
          Session log
        </button>
      </div>

      <div className="overflow-hidden rounded-lg border border-gray-200 dark:border-white/10">
        {visible.map((row, index) => {
          const open = selectedId === row.id;
          const startsTurn = index === 0 || visible[index - 1].turnNo !== row.turnNo;
          return (
            <div
              key={row.id}
              className="border-b border-gray-100 dark:border-white/5 last:border-b-0"
            >
              {startsTurn && rows.some((other) => other.turnNo !== row.turnNo) ? (
                <p className="bg-gray-50 dark:bg-gray-950 px-3 py-1 text-[11px] font-medium uppercase tracking-wide text-gray-400 dark:text-gray-500">
                  Turn {row.turnNo}
                </p>
              ) : null}
              <button
                type="button"
                onClick={() => setSelectedId(open ? null : row.id)}
                aria-expanded={open}
                className={`flex w-full items-baseline gap-2 px-3 py-1.5 text-left text-[12px] transition-colors hover:bg-gray-50 dark:hover:bg-white/5 ${
                  open ? 'bg-gray-50 dark:bg-gray-950' : ''
                }`}
              >
                <span
                  className={`w-16 shrink-0 rounded px-1.5 py-0.5 text-center text-[10px] font-medium uppercase tracking-wide ${ACTOR_TONE[row.actor]}`}
                >
                  {row.actor}
                </span>
                <span
                  className={`w-28 shrink-0 truncate font-medium ${row.isError ? 'text-rose-600 dark:text-rose-400' : 'text-gray-800 dark:text-gray-200'}`}
                >
                  {row.label}
                </span>
                <span className="min-w-0 flex-1 truncate font-mono text-gray-500 dark:text-gray-400">
                  {row.detail}
                </span>
                {row.durationMs !== undefined ? (
                  <span className="shrink-0 tabular-nums text-gray-400 dark:text-gray-500">
                    {formatDuration(row.durationMs)}
                  </span>
                ) : null}
              </button>
              {open ? (
                <RowDetail
                  row={row}
                  resultRow={row.resultOf ? byId.get(row.resultOf) : undefined}
                />
              ) : null}
            </div>
          );
        })}
        {visible.length === 0 ? (
          <p className="px-3 py-6 text-center text-[12px] text-gray-500 dark:text-gray-400">
            No event matches “{query}”.
          </p>
        ) : null}
      </div>
    </section>
  );
}
