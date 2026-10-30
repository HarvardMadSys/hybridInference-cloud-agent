'use client';

// The legend beside a ring: one row per slice, hover synced with the ring.
// Adapted from dsh-context `sliceList.tsx` / `tokenRows.ts` (Apache-2.0,
// commit 32982aa). A row may carry no share at all ("not recorded"): it has no
// arc, and is never drawn as 0%.

export interface SliceRow {
  key: string;
  bg: string;
  label: string;
  /** e.g. "≈12.3K"; empty when nothing is known. */
  count: string;
  /** e.g. "34%"; null when the row has no share. */
  pct: string | null;
  tip?: string;
  dim?: boolean;
}

export function SliceList({
  rows,
  hoverKey,
  onHoverKey,
  label,
}: {
  rows: SliceRow[];
  hoverKey?: string | null;
  onHoverKey?: (key: string | null) => void;
  label: string;
}) {
  return (
    <ul aria-label={label} className="min-w-0 flex-1 space-y-0.5 text-xs">
      {rows.map((row) => (
        <li
          key={row.key}
          title={row.tip}
          onMouseEnter={() => onHoverKey?.(row.key)}
          onMouseLeave={() => onHoverKey?.(null)}
          className={`flex items-center justify-between gap-2 rounded px-1 py-0.5 ${
            hoverKey === row.key ? 'bg-surface-hover' : ''
          } ${row.dim ? 'opacity-60' : ''}`}
        >
          <span className="flex min-w-0 items-center gap-1.5 text-muted">
            <span
              aria-hidden="true"
              className={`h-2 w-2 shrink-0 rounded-full ${row.pct === null ? 'border border-dashed border-muted bg-transparent' : row.bg}`}
            />
            <span className="truncate">{row.label}</span>
          </span>
          <span className="shrink-0 tabular-nums text-foreground">
            {row.count}
            {row.pct !== null ? <span className="ml-1.5 text-muted">{row.pct}</span> : null}
          </span>
        </li>
      ))}
    </ul>
  );
}
