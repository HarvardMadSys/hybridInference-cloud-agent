// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/lib/api/agents', () => ({ getAgentJobImage: vi.fn() }));

import { getAgentJobImage } from '@/lib/api/agents';
import { LargeFilePreview, looksLikeImage } from './WorkspaceImage';

beforeEach(() => {
  vi.mocked(getAgentJobImage).mockReset();
  URL.createObjectURL = vi.fn(() => 'blob:large');
  URL.revokeObjectURL = vi.fn();
});

afterEach(cleanup);

describe('large file preview', () => {
  it('reads a large image through the image route', async () => {
    vi.mocked(getAgentJobImage).mockResolvedValue(new Blob(['png'], { type: 'image/png' }));

    render(<LargeFilePreview jobId="job-1" path="shots/full-page.png" fallback="Too large" />);

    expect(await screen.findByRole('img', { name: 'Preview of full-page.png' })).toHaveAttribute(
      'src',
      'blob:large',
    );
    expect(getAgentJobImage).toHaveBeenCalledWith(
      'job-1',
      'shots/full-page.png',
      expect.any(AbortSignal),
    );
  });

  it('keeps the size message for a file that is not an image', () => {
    render(<LargeFilePreview jobId="job-1" path="dump.log" fallback="Too large" />);

    expect(screen.getByText('Too large')).toBeInTheDocument();
    expect(getAgentJobImage).not.toHaveBeenCalled();
  });

  it('falls back when the image cannot be read either', async () => {
    vi.mocked(getAgentJobImage).mockRejectedValue(new Error('image_too_large'));

    render(<LargeFilePreview jobId="job-1" path="huge.png" fallback="Too large" />);

    expect(await screen.findByText('Too large')).toBeInTheDocument();
  });

  it('goes by the name only to decide whether to try', () => {
    expect(looksLikeImage('a/b.PNG')).toBe(true);
    expect(looksLikeImage('a/b.jpeg')).toBe(true);
    expect(looksLikeImage('a/b.svg')).toBe(false);
  });
});
