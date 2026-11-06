// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TurnDeliverables } from './TurnDeliverables';
import type { RecordedTurnOutputs } from './recordedTurnOutputs';

afterEach(cleanup);

const JOB = { id: 'history-turn', state: 'done' as const };
const OUTPUTS: RecordedTurnOutputs = {
  jobId: JOB.id,
  files: Array.from({ length: 6 }, (_, index) => ({
    path: `src/file-${index + 1}.tsx`,
    add: index + 1,
    del: 2,
    lines: [{ marker: 'add', text: `+recorded line ${index + 1}` }],
  })),
  prUrl: 'https://github.com/example/repo/pull/12',
};

describe('turn deliverables', () => {
  it('opens the selected file with its recorded lines and producing job identity', () => {
    const onOpenFile = vi.fn();
    render(<TurnDeliverables job={JOB} outputs={OUTPUTS} onOpenFile={onOpenFile} />);
    const file = screen.getByRole('button', { name: 'Open recorded diff for src/file-3.tsx' });
    file.focus();
    expect(file).toHaveFocus();
    expect(file).toHaveAttribute('type', 'button');
    fireEvent.click(file);
    expect(onOpenFile).toHaveBeenCalledExactlyOnceWith({
      jobId: 'history-turn',
      file: OUTPUTS.files[2],
    });
  });

  it('shows four files initially and retains the full set behind an accessible disclosure', () => {
    render(<TurnDeliverables job={JOB} outputs={OUTPUTS} onOpenFile={vi.fn()} />);
    expect(screen.getAllByRole('button', { name: /^Open recorded diff/ })).toHaveLength(4);
    expect(screen.getByText('src/file-6.tsx')).not.toBeVisible();
    const show = screen.getByRole('button', { name: 'Show all 6 files' });
    expect(show).toHaveAttribute('aria-expanded', 'false');
    expect(document.getElementById(show.getAttribute('aria-controls')!)).not.toBeNull();
    fireEvent.click(show);
    expect(screen.getAllByRole('button', { name: /^Open recorded diff/ })).toHaveLength(6);
    const hide = screen.getByRole('button', { name: 'Show fewer files' });
    expect(hide).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(hide);
    expect(screen.getByText('src/file-6.tsx')).not.toBeVisible();
    expect(screen.getByText('src/file-6.tsx')).toBeInTheDocument();
  });

  it('does not carry an expanded historical file list into another turn', () => {
    const { rerender } = render(
      <TurnDeliverables job={JOB} outputs={OUTPUTS} onOpenFile={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Show all 6 files' }));
    rerender(
      <TurnDeliverables
        job={{ ...JOB, id: 'another-turn' }}
        outputs={{ ...OUTPUTS, jobId: 'another-turn' }}
        onOpenFile={vi.fn()}
      />,
    );
    expect(screen.getAllByRole('button', { name: /^Open recorded diff/ })).toHaveLength(4);
  });

  it.each(['queued', 'running', 'publishing', undefined] as const)(
    'does not render recorded outputs for unsettled state %s',
    (state) => {
      const { container } = render(
        <TurnDeliverables job={{ ...JOB, state }} outputs={OUTPUTS} onOpenFile={vi.fn()} />,
      );
      expect(container).toBeEmptyDOMElement();
    },
  );

  it('shows no card for absent outputs or evidence belonging to another turn', () => {
    const { container, rerender } = render(<TurnDeliverables job={JOB} onOpenFile={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
    rerender(
      <TurnDeliverables
        job={JOB}
        outputs={{ ...OUTPUTS, jobId: 'latest-live-turn' }}
        onOpenFile={vi.fn()}
      />,
    );
    expect(container).toBeEmptyDOMElement();
    rerender(
      <TurnDeliverables job={JOB} outputs={{ jobId: JOB.id, files: [] }} onOpenFile={vi.fn()} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders a recorded PR action independently from file output', () => {
    render(<TurnDeliverables job={JOB} outputs={{ ...OUTPUTS, files: [] }} onOpenFile={vi.fn()} />);
    const link = screen.getByRole('link', { name: 'Open pull request' });
    expect(link).toHaveAttribute('href', OUTPUTS.prUrl);
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.queryByText('Recorded changes')).not.toBeInTheDocument();
  });

  it('does not invent line counts when a recorded binary diff has no statistics', () => {
    render(
      <TurnDeliverables
        job={JOB}
        outputs={{
          jobId: JOB.id,
          files: [{ path: 'output.png', lines: [{ marker: 'ctx', text: 'Binary files differ' }] }],
        }}
        onOpenFile={vi.fn()}
      />,
    );
    expect(screen.getByText('Line changes not recorded')).toBeInTheDocument();
    expect(screen.queryByText('+0')).not.toBeInTheDocument();
  });

  it('keeps existing file actions while rejecting an unsafe PR URL', () => {
    render(
      <TurnDeliverables
        job={JOB}
        outputs={{ ...OUTPUTS, prUrl: 'javascript:alert(1)' }}
        onOpenFile={vi.fn()}
      />,
    );
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /^Open recorded diff/ })).toHaveLength(4);
  });
});
