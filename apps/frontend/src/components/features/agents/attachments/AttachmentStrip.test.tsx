// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AttachmentStrip, groupAttachments } from './AttachmentStrip';
import type { AttachmentEntry } from './useAttachments';

afterEach(() => cleanup());

function entry(overrides: Partial<AttachmentEntry> & { key: string }): AttachmentEntry {
  return { path: 'a.txt', size: 10, status: 'ready', ...overrides };
}

describe('groupAttachments', () => {
  it('shows a folder once rather than once per file inside it', () => {
    const groups = groupAttachments([
      entry({ key: '1', path: 'specs/api.md', size: 100 }),
      entry({ key: '2', path: 'specs/diagrams/flow.svg', size: 200 }),
      entry({ key: '3', path: 'notes.md', size: 50 }),
    ]);
    expect(groups.map((group) => group.label)).toEqual(['specs/', 'notes.md']);
    expect(groups[0]!.members).toHaveLength(2);
    expect(groups[0]!.bytes).toBe(300);
  });

  it('keeps a rejected file out of its folder so its reason stays visible', () => {
    const groups = groupAttachments([
      entry({ key: '1', path: 'specs/api.md' }),
      entry({ key: '2', path: 'specs/huge.bin', status: 'rejected', error: 'over 5 MB' }),
    ]);
    expect(groups.map((group) => group.label)).toEqual(['specs/', 'specs/huge.bin']);
  });

  it('preserves the order files were picked in', () => {
    const groups = groupAttachments([
      entry({ key: '1', path: 'b.txt' }),
      entry({ key: '2', path: 'a.txt' }),
    ]);
    expect(groups.map((group) => group.label)).toEqual(['b.txt', 'a.txt']);
  });
});

describe('AttachmentStrip', () => {
  it('renders nothing when nothing is attached', () => {
    const { container } = render(<AttachmentStrip entries={[]} onRemove={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('states why a rejected file was not attached, beside that file', () => {
    render(
      <AttachmentStrip
        entries={[entry({ key: '1', path: 'dump.sql', status: 'rejected', error: 'over 5 MB' })]}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.getByText('dump.sql')).toBeInTheDocument();
    expect(screen.getByText('over 5 MB')).toBeInTheDocument();
  });

  it('warns about credentials once something is actually attached', () => {
    const warning = /Do not upload credentials/;
    const { rerender } = render(
      <AttachmentStrip
        entries={[entry({ key: '1', status: 'rejected', error: 'over 5 MB' })]}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.queryByText(warning)).not.toBeInTheDocument();
    rerender(<AttachmentStrip entries={[entry({ key: '1' })]} onRemove={vi.fn()} />);
    expect(screen.getByText(warning)).toBeInTheDocument();
  });

  it('removes every file of a folder when its chip is dismissed', () => {
    const removed = vi.fn();
    render(
      <AttachmentStrip
        entries={[
          entry({ key: '1', path: 'specs/api.md' }),
          entry({ key: '2', path: 'specs/flow.svg' }),
        ]}
        onRemove={removed}
      />,
    );
    screen.getByRole('button', { name: 'Remove specs/' }).click();
    expect(removed.mock.calls.map(([key]) => key)).toEqual(['1', '2']);
  });
});
