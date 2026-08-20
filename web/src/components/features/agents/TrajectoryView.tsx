'use client';

// The trajectory: every stored event of a turn, in order, with the payload
// behind each one.
//
// The chat tab beside this is a reading of the stream. This is the stream —
// unfolded, undeduplicated, including the rows no view knows how to render.
// It exists for the question the chat view cannot answer: not "what did the
// agent say" but "what actually happened, and how long did each step take".

import { useMemo, useState } from 'react';

import type { AgentJobEventApi } from '@/lib/api/agents';
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
  { key: 'other', label: 'Other', tone: 'bg-gray-300' },
];

const ACTOR_TONE: Record<TrajectoryRow['actor'], string> = {
  user: 'bg-sky-50 text-sky-700',
  assistant: 'bg-violet-50 text-violet-700',
  tool: 'bg-amber-50 text-amber-700',
  context: 'bg-gray-100 text-gray-600',
  system: 'bg-gray-100 text-gray-600',
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
  return (
    <div className="space-y-1 overflow-x-auto pb-1">
      {LANES.filter((lane) => rows.some((row) => row.lane === lane.key)).map((lane) => (
        <div key={lane.key} className="flex items-center gap-2">
          <span className="w-12 shrink-0 text-right text-[10px] uppercase tracking-wide text-gray-400">
            {lane.label}
          </span>
          <div className="flex gap-px">
            {rows.map((row) =>
              row.lane === lane.key ? (
                <button
                  key={row.id}
                  type="button"
                  aria-label={`${row.label} (${row.eventType})`}
                  onClick={() => onSelect(row.id)}
                  className={`h-2.5 w-2 shrink-0 rounded-[1px] transition-opacity hover:opacity-100 ${
                    row.isError ? 'bg-rose-400' : lane.tone
                  } ${selectedId === row.id ? 'opacity-100 ring-1 ring-gray-800' : 'opacity-70'}`}
                />
              ) : (
                <span key={row.id} className="h-2.5 w-2 shrink-0" />
              ),
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/** What one event carried, shown only when a reader asks for it. */
function RowDetail({ row, resultRow }: { row: TrajectoryRow; resultRow?: TrajectoryRow }) {
  return (
    <div className="space-y-3 border-t border-gray-200 bg-white px-3 py-3 text-[12px]">
      <dl className="grid grid-cols-[6rem_1fr] gap-x-3 gap-y-1">
        <dt className="text-gray-500">Event</dt>
        <dd className="font-mono text-gray-800">
          {row.eventType} · seq {row.seq} · attempt {row.attemptId}
        </dd>
        <dt className="text-gray-500">Time</dt>
        <dd className="text-gray-800">{row.at ?? 'not recorded'}</dd>
        {row.durationMs !== undefined ? (
          <>
            <dt className="text-gray-500">Duration</dt>
            <dd className="text-gray-800">
              {formatDuration(row.durationMs)}{' '}
              <span className="text-gray-500">
                — from the request to its result, so it includes the model&rsquo;s own thinking
              </span>
            </dd>
          </>
        ) : null}
      </dl>
      <div>
        <p className="mb-1 font-medium text-gray-600">Payload</p>
        <pre className="max-h-64 overflow-auto rounded bg-gray-50 p-2 font-mono text-[11px] leading-relaxed text-gray-800">
          {JSON.stringify(row.payload, null, 2)}
        </pre>
      </div>
      {resultRow ? (
        <div>
          <p className="mb-1 font-medium text-gray-600">Result</p>
          <pre className="max-h-64 overflow-auto rounded bg-gray-50 p-2 font-mono text-[11px] leading-relaxed text-gray-800">
            {JSON.stringify(resultRow.payload, null, 2)}
          </pre>
        </div>
      ) : null}
    </div>
  );
}

export function TrajectoryView({ events }: { events: AgentJobEventApi[] }) {
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
      <p className="py-8 text-center text-[13px] text-gray-500">
        This turn stored no events. A turn that ran before event capture, or one that never started,
        has nothing to show here.
      </p>
    );
  }

  return (
    <section aria-label="Trajectory" className="space-y-3">
      <Minimap rows={rows} selectedId={selectedId} onSelect={setSelectedId} />

      <div className="flex items-center gap-2">
        <input
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search events"
          aria-label="Search events"
          className="min-w-0 flex-1 rounded-lg border border-gray-200 px-2.5 py-1.5 text-[13px] outline-none focus:border-gray-300"
        />
        <span className="shrink-0 text-[12px] tabular-nums text-gray-500">
          {visible.length === rows.length
            ? `${rows.length} events`
            : `${visible.length} of ${rows.length}`}
        </span>
        <button
          type="button"
          onClick={download}
          className="shrink-0 rounded-lg border border-gray-200 px-2.5 py-1.5 text-[12px] text-gray-600 transition-colors hover:border-gray-300 hover:text-gray-900"
        >
          Session log
        </button>
      </div>

      <div className="overflow-hidden rounded-lg border border-gray-200">
        {visible.map((row) => {
          const open = selectedId === row.id;
          return (
            <div key={row.id} className="border-b border-gray-100 last:border-b-0">
              <button
                type="button"
                onClick={() => setSelectedId(open ? null : row.id)}
                aria-expanded={open}
                className={`flex w-full items-baseline gap-2 px-3 py-1.5 text-left text-[12px] transition-colors hover:bg-gray-50 ${
                  open ? 'bg-gray-50' : ''
                }`}
              >
                <span
                  className={`w-16 shrink-0 rounded px-1.5 py-0.5 text-center text-[10px] font-medium uppercase tracking-wide ${ACTOR_TONE[row.actor]}`}
                >
                  {row.actor}
                </span>
                <span
                  className={`w-28 shrink-0 truncate font-medium ${row.isError ? 'text-rose-600' : 'text-gray-800'}`}
                >
                  {row.label}
                </span>
                <span className="min-w-0 flex-1 truncate font-mono text-gray-500">
                  {row.detail}
                </span>
                {row.durationMs !== undefined ? (
                  <span className="shrink-0 tabular-nums text-gray-400">
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
          <p className="px-3 py-6 text-center text-[12px] text-gray-500">
            No event matches “{query}”.
          </p>
        ) : null}
      </div>
    </section>
  );
}
