'use client';

/**
 * A progress bar with a small pixel dinosaur running along it.
 *
 * Shown while every server downloads and checks a new agent version. The bar
 * and the dinosaur glide between the numbers the servers report, so a long
 * download still looks alive without the page inventing progress; the legs
 * stop under `prefers-reduced-motion`, which the global stylesheet applies to
 * every animation.
 */

/** 21 × 15 body, facing right; `.` is empty, `#` is filled (the eye is a hole). */
const BODY = [
  '..........########...',
  '.........##.#######..',
  '.........##########..',
  '.........##########..',
  '.........#####.......',
  '.........########....',
  '#.......#####........',
  '#......#######.......',
  '##....#########......',
  '###..##########.#....',
  '###############......',
  '.#############.......',
  '..###########........',
  '...#########.........',
  '....#######..........',
];
/** Two strides, one leg lifted in each. */
const STRIDE_A = ['.....##..##..........', '.....##..............', '.....###.............'];
const STRIDE_B = ['.....##..##..........', '.........##..........', '.........###.........'];

/** One path for a pixel grid: a rectangle per horizontal run of `#`. */
function pixels(rows: string[], top = 0): string {
  const parts: string[] = [];
  rows.forEach((row, y) => {
    for (const match of row.matchAll(/#+/g)) {
      const x = match.index ?? 0;
      parts.push(`M${x} ${y + top}h${match[0].length}v1h-${match[0].length}z`);
    }
  });
  return parts.join('');
}

const BODY_PATH = pixels(BODY);
const STRIDE_A_PATH = pixels(STRIDE_A, BODY.length);
const STRIDE_B_PATH = pixels(STRIDE_B, BODY.length);
const HEIGHT = BODY.length + STRIDE_A.length;
const WIDTH = BODY[0].length;

export default function DinoProgress({ progress, label }: { progress: number; label: string }) {
  const value = Math.min(100, Math.max(0, Math.round(progress)));
  return (
    <div className="mt-3 min-w-0">
      <div className="flex items-baseline justify-between gap-3 text-xs text-muted">
        <span className="min-w-0 truncate">{label}</span>
        <span className="shrink-0 tabular-nums">{value}%</span>
      </div>
      <div
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={value}
        className="relative mt-1 h-9"
      >
        <div className="absolute inset-x-0 bottom-0 h-1 rounded-full bg-surface-muted" />
        <div
          className="absolute bottom-0 left-0 h-1 rounded-full bg-accent transition-[width] duration-1000 ease-out"
          style={{ width: `${value}%` }}
        />
        {/* Its nose reaches the end of the bar at 100%, never past it. */}
        <div
          className="absolute bottom-1 transition-[left] duration-1000 ease-out"
          style={{ left: `calc(${value}% - ${value / 100} * 1.75rem)` }}
        >
          <svg
            viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
            className="h-6 w-7 text-foreground"
            shapeRendering="crispEdges"
            aria-hidden="true"
          >
            <path d={BODY_PATH} fill="currentColor" />
            <path d={STRIDE_A_PATH} className="ui-dino-stride-a" fill="currentColor" />
            <path d={STRIDE_B_PATH} className="ui-dino-stride-b" fill="currentColor" />
          </svg>
        </div>
      </div>
    </div>
  );
}
