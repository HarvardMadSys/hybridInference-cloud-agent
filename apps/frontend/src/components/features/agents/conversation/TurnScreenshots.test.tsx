// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/agents', () => ({
  listAgentJobArtifactImages: vi.fn(),
  getAgentJobImage: vi.fn(),
}));

import { getAgentJobImage, listAgentJobArtifactImages } from '@/lib/api/agents';
import { clearScreenshotListings, TurnScreenshots, turnImages } from './TurnScreenshots';

const STARTED = '2026-11-05T10:00:00.000Z';
const FINISHED = '2026-11-05T10:05:00.000Z';
const at = (iso: string) => Date.parse(iso) / 1000;

const JOB = {
  id: 'turn-2',
  threadId: 'thread-1',
  state: 'done' as const,
  startedAt: STARTED,
  finishedAt: FINISHED,
};

function image(name: string, iso: string) {
  return { path: `.cloud-agent-artifacts/playwright-mcp/${name}`, size: 100, modified_at: at(iso) };
}

beforeEach(() => {
  clearScreenshotListings();
  vi.mocked(listAgentJobArtifactImages).mockReset();
  vi.mocked(getAgentJobImage).mockReset();
  vi.mocked(getAgentJobImage).mockResolvedValue(new Blob(['png'], { type: 'image/png' }));
  let next = 0;
  URL.createObjectURL = vi.fn(() => `blob:image-${(next += 1)}`);
  URL.revokeObjectURL = vi.fn();
});

afterEach(cleanup);

describe('turn images', () => {
  it('keeps the images written while the turn ran, oldest first', () => {
    const images = [
      image('after.png', '2026-11-05T10:07:00.000Z'),
      image('late.png', '2026-11-05T10:04:00.000Z'),
      image('early.png', '2026-11-05T10:01:00.000Z'),
      image('before.png', '2026-11-05T09:50:00.000Z'),
    ];

    expect(turnImages(images, STARTED, FINISHED).map((item) => item.path)).toEqual([
      '.cloud-agent-artifacts/playwright-mcp/early.png',
      '.cloud-agent-artifacts/playwright-mcp/late.png',
    ]);
  });

  it('allows a few seconds of clock slack at either end', () => {
    const images = [
      image('just-before.png', '2026-11-05T09:59:59.000Z'),
      image('just-after.png', '2026-11-05T10:05:04.000Z'),
    ];

    expect(turnImages(images, STARTED, FINISHED)).toHaveLength(2);
  });
});

describe('turn screenshots', () => {
  it('shows the turn’s screenshots and opens one large', async () => {
    vi.mocked(listAgentJobArtifactImages).mockResolvedValue({
      images: [image('page.png', '2026-11-05T10:02:00.000Z')],
      truncated: false,
    });

    render(<TurnScreenshots job={JOB} />);

    const open = await screen.findByRole('button', { name: 'Open page.png' });
    await waitFor(() => expect(open).toBeEnabled());
    expect(listAgentJobArtifactImages).toHaveBeenCalledExactlyOnceWith('turn-2', 0);
    expect(getAgentJobImage).toHaveBeenCalledWith(
      'turn-2',
      '.cloud-agent-artifacts/playwright-mcp/page.png',
      expect.any(AbortSignal),
    );

    fireEvent.click(open);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveTextContent('.cloud-agent-artifacts/playwright-mcp/page.png');
    expect(screen.getByRole('img', { name: 'Preview of page.png' })).toHaveAttribute(
      'src',
      'blob:image-1',
    );
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('asks once for every turn of a conversation', async () => {
    vi.mocked(listAgentJobArtifactImages).mockResolvedValue({
      images: [image('page.png', '2026-11-05T10:02:00.000Z')],
      truncated: false,
    });
    const earlier = {
      ...JOB,
      id: 'turn-1',
      startedAt: '2026-11-05T09:00:00.000Z',
      finishedAt: '2026-11-05T09:10:00.000Z',
    };

    render(
      <>
        <TurnScreenshots job={earlier} />
        <TurnScreenshots job={JOB} />
      </>,
    );

    expect(await screen.findAllByRole('button', { name: 'Open page.png' })).toHaveLength(1);
    expect(listAgentJobArtifactImages).toHaveBeenCalledTimes(1);
  });

  it('shows the latest six and says how many earlier ones there are', async () => {
    const images = Array.from({ length: 8 }, (_, index) =>
      image(`shot-${index}.png`, `2026-11-05T10:0${index % 5}:0${index}.000Z`),
    );
    vi.mocked(listAgentJobArtifactImages).mockResolvedValue({ images, truncated: false });

    render(<TurnScreenshots job={JOB} />);

    expect(await screen.findAllByRole('button', { name: /^Open shot-/ })).toHaveLength(6);
    expect(
      screen.getByText('2 earlier screenshots are in the workspace files.'),
    ).toBeInTheDocument();
  });

  it('asks nothing for a turn still running, and shows nothing without a workspace', async () => {
    const { rerender } = render(
      <TurnScreenshots job={{ ...JOB, state: 'running', finishedAt: null }} />,
    );
    expect(listAgentJobArtifactImages).not.toHaveBeenCalled();

    vi.mocked(listAgentJobArtifactImages).mockRejectedValue(new Error('workspace_unavailable'));
    rerender(<TurnScreenshots job={JOB} />);

    await waitFor(() => expect(listAgentJobArtifactImages).toHaveBeenCalledTimes(1));
    expect(screen.queryByRole('region', { name: 'Screenshots' })).not.toBeInTheDocument();
  });
});
