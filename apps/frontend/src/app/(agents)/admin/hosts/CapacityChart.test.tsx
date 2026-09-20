// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { AgentHostCapacityPoint } from '@/lib/api/admin';

import CapacityChart, { type CapacityChartProps } from './CapacityChart';
import { CHART_PLOT_HEIGHT, CHART_VIEW, chartXForTime } from './capacity';

const GIB = 1024 ** 3;
const SINCE = '2026-09-11T12:00:00Z';
const UNTIL = '2026-09-11T12:05:00Z';

const at = (minute: number, second = 30): string =>
  new Date(Date.UTC(2026, 8, 11, 12, minute, second)).toISOString();

function point(
  minute: number,
  cpu: number | null,
  over: Partial<AgentHostCapacityPoint> = {},
): AgentHostCapacityPoint {
  return {
    bucket_at: at(minute, 0),
    sampled_at: cpu === null ? null : at(minute),
    linux_boot_id: 'boot-a',
    cpu_percent: cpu,
    cpu_logical_count: 32,
    cpu_window_seconds: 30,
    cpu_missing_reason: null,
    memory_total_bytes: 128 * GIB,
    memory_available_bytes: 86 * GIB,
    memory_used_bytes: 42 * GIB,
    memory_used_percent: cpu === null ? null : 32.8,
    memory_missing_reason: null,
    ...over,
  };
}

function props(over: Partial<CapacityChartProps> = {}): CapacityChartProps {
  return {
    metric: 'cpu',
    title: 'CPU usage (%)',
    points: [point(0, 10), point(1, 20), point(2, 30), point(3, 40), point(4, 50), point(5, 60)],
    since: SINCE,
    until: UNTIL,
    resolutionSeconds: 60,
    range: '1h',
    stale: false,
    ...over,
  };
}

/** The x of one plotted minute, in the chart's own user units. */
function xOf(minute: number): number {
  return chartXForTime(Date.parse(at(minute, 0)), Date.parse(SINCE), Date.parse(UNTIL));
}

afterEach(cleanup);

describe('CapacityChart', () => {
  it('joins consecutive observed minutes into one line and nothing else', () => {
    const { container } = render(
      <CapacityChart
        {...props({
          points: [
            point(0, 10),
            point(1, 20),
            point(2, null, { cpu_missing_reason: 'warming_up' }),
            point(3, 40),
            point(4, 50),
            point(5, null, { cpu_missing_reason: 'source_unavailable' }),
          ],
        })}
      />,
    );

    const lines = [...container.querySelectorAll('polyline')];
    expect(lines).toHaveLength(2);
    expect(lines[0].getAttribute('points')?.split(' ')).toHaveLength(2);
    expect(lines[1].getAttribute('points')?.split(' ')).toHaveLength(2);
    // The gap is named rather than crossed.
    expect(screen.getByText('No data')).toBeInTheDocument();
  });

  it('draws an isolated observed minute as a point, not as nothing', () => {
    const { container } = render(
      <CapacityChart
        {...props({
          points: [point(0, null), point(1, 20), point(2, null), point(3, null)],
        })}
      />,
    );
    expect(container.querySelectorAll('polyline')).toHaveLength(0);
    const dots = [...container.querySelectorAll('circle')];
    expect(dots).toHaveLength(1);
    expect(dots[0]).toHaveAttribute('cx', String(xOf(1)));
  });

  it('spans the axis from since to until on a fixed 0-100 scale', () => {
    const { container } = render(<CapacityChart {...props()} />);
    const line = container.querySelector('polyline');
    const plotted = (line?.getAttribute('points') ?? '').split(' ');
    const first = plotted[0];
    const last = plotted.at(-1) ?? '';
    expect(Number(first.split(',')[0])).toBe(xOf(0));
    expect(Number(last.split(',')[0])).toBe(xOf(5));
    expect(screen.getByText('0%')).toBeInTheDocument();
    expect(screen.getByText('100%')).toBeInTheDocument();
    // The zone is stated, not implied.
    expect(screen.getByText('UTC')).toBeInTheDocument();
    expect(screen.getByText('12:00')).toBeInTheDocument();
    expect(screen.getByText('12:05')).toBeInTheDocument();
    // Only the two ends: the middle of the window is where the axis title
    // sits, and a tick there put the title on a second line under it.
    expect(screen.queryByText('12:02')).not.toBeInTheDocument();
    expect(CHART_PLOT_HEIGHT).toBeGreaterThan(0);
  });

  it('does not extend a stale curve to now', () => {
    const { container } = render(
      <CapacityChart
        {...props({
          stale: true,
          points: [point(0, 10), point(1, 20), point(2, null), point(3, null), point(4, null)],
        })}
      />,
    );
    const line = container.querySelector('polyline');
    const lastX = Number((line?.getAttribute('points') ?? '').split(' ').at(-1)?.split(',')[0]);
    // The last observation is at 12:01, and the axis ends at 12:05.
    expect(lastX).toBe(xOf(1));
    expect(lastX).toBeLessThan(CHART_VIEW.width - CHART_VIEW.right);
  });

  it('says history is accumulating rather than drawing an empty axis as zero', () => {
    const { container, unmount } = render(
      <CapacityChart {...props({ points: [point(0, null), point(1, null)] })} />,
    );
    expect(screen.getAllByText('History is accumulating').length).toBeGreaterThan(0);
    expect(container.querySelectorAll('polyline')).toHaveLength(0);
    unmount();

    render(<CapacityChart {...props({ points: [point(0, null)], stale: true })} />);
    expect(screen.getByText('No data in this range')).toBeInTheDocument();
  });

  it('names the curve and describes it in text as well as in ink', () => {
    render(<CapacityChart {...props()} />);
    const chart = screen.getByRole('img', { name: 'CPU usage (%)' });
    expect(chart).toBeInTheDocument();
    expect(screen.getByText(/6 of 6 minutes observed/)).toBeInTheDocument();
    expect(screen.getByText(/Highest observed in range: 60.0%/)).toBeInTheDocument();
  });

  it('walks the observed points with the keyboard and announces each one', () => {
    render(<CapacityChart {...props()} />);
    const slider = screen.getByRole('slider', { name: 'CPU usage (%) point selector' });
    expect(slider).toHaveAttribute('aria-valuemax', '5');

    // Focusing an unfocused chart lands on the newest point.
    act(() => slider.focus());
    expect(slider).toHaveAttribute('aria-valuenow', '5');
    expect(slider).toHaveAttribute('aria-valuetext', '2026-09-11 12:05 UTC · CPU 60.0%');

    fireEvent.keyDown(slider, { key: 'ArrowLeft' });
    expect(slider).toHaveAttribute('aria-valuenow', '4');
    expect(screen.getByText('2026-09-11 12:04 UTC · CPU 50.0%')).toBeInTheDocument();

    fireEvent.keyDown(slider, { key: 'Home' });
    expect(slider).toHaveAttribute('aria-valuenow', '0');
    expect(slider).toHaveAttribute('aria-valuetext', '2026-09-11 12:00 UTC · CPU 10.0%');

    // Past either end it stays on the axis rather than wrapping.
    fireEvent.keyDown(slider, { key: 'ArrowLeft' });
    expect(slider).toHaveAttribute('aria-valuenow', '0');
    fireEvent.keyDown(slider, { key: 'End' });
    fireEvent.keyDown(slider, { key: 'ArrowRight' });
    expect(slider).toHaveAttribute('aria-valuenow', '5');
  });

  it('selects the nearest point under a pointer', () => {
    render(<CapacityChart {...props()} />);
    const slider = screen.getByRole('slider', { name: 'CPU usage (%) point selector' });
    vi.spyOn(slider, 'getBoundingClientRect').mockReturnValue({
      left: 0,
      top: 0,
      right: CHART_VIEW.width,
      bottom: CHART_VIEW.height,
      width: CHART_VIEW.width,
      height: CHART_VIEW.height,
      x: 0,
      y: 0,
      toJSON: () => ({}),
    });

    fireEvent.pointerMove(slider, { clientX: xOf(3) + 4 });
    expect(slider).toHaveAttribute('aria-valuenow', '3');
    expect(screen.getByText('2026-09-11 12:03 UTC · CPU 40.0%')).toBeInTheDocument();

    // Touch selects the same way a pointer does.
    fireEvent.pointerDown(slider, { clientX: xOf(1) - 4 });
    expect(slider).toHaveAttribute('aria-valuenow', '1');
  });

  it('shows the absolute pair beside a memory percentage', () => {
    render(<CapacityChart {...props({ metric: 'memory', title: 'Memory usage (%)' })} />);
    const slider = screen.getByRole('slider', { name: 'Memory usage (%) point selector' });
    act(() => slider.focus());
    expect(slider).toHaveAttribute(
      'aria-valuetext',
      '2026-09-11 12:05 UTC · Memory 32.8% · 42 / 128 GiB',
    );
  });

  it('keeps the reading inside the plot at either end of the axis', () => {
    render(<CapacityChart {...props()} />);
    const slider = screen.getByRole('slider', { name: 'CPU usage (%) point selector' });
    const bubble = (text: string): Element => screen.getByText(text);

    // Centred on the oldest point, half the bubble was outside the panel and
    // the panel clipped it mid-word — it read "026-09-11 08:58 UTC".
    fireEvent.keyDown(slider, { key: 'Home' });
    expect(bubble('2026-09-11 12:00 UTC · CPU 10.0%').className).toContain('translate-x-0');

    fireEvent.keyDown(slider, { key: 'End' });
    expect(bubble('2026-09-11 12:05 UTC · CPU 60.0%').className).toContain('-translate-x-full');

    // Away from the ends it still points at its own minute.
    fireEvent.keyDown(slider, { key: 'ArrowLeft' });
    fireEvent.keyDown(slider, { key: 'ArrowLeft' });
    expect(bubble('2026-09-11 12:03 UTC · CPU 40.0%').className).toContain('-translate-x-1/2');
  });

  it('keeps a gap out of the line even when both of its edges are observed', () => {
    const { container } = render(
      <CapacityChart
        {...props({
          metric: 'memory',
          title: 'Memory usage (%)',
          points: [
            point(0, 10),
            point(1, 10),
            point(2, null, { sampled_at: null, memory_used_percent: null }),
            point(3, 10),
            point(4, 10),
            point(5, 10),
          ],
        })}
      />,
    );
    const lines = [...container.querySelectorAll('polyline')];
    expect(lines).toHaveLength(2);
    // Neither line reaches into the unobserved minute.
    const xs = lines.map((line) => (line.getAttribute('points') ?? '').split(' '));
    expect(Number(xs[0].at(-1)?.split(',')[0])).toBe(xOf(1));
    expect(Number(xs[1][0].split(',')[0])).toBe(xOf(3));

    // One wash per run, drawn from the run's own points down to the baseline,
    // so the fill breaks with the line. A single wash across both runs would
    // shade the minute nobody observed as though something had been measured.
    const washes = [...container.querySelectorAll('polygon')].map((wash) =>
      (wash.getAttribute('points') ?? '').split(' '),
    );
    expect(washes).toHaveLength(2);
    expect(Number(washes[0][washes[0].length - 2].split(',')[0])).toBe(xOf(1));
    expect(Number(washes[1][0].split(',')[0])).toBe(xOf(3));
  });
});
