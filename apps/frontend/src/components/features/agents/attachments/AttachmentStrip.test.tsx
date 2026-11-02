// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
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

  it('shows upload progress and the ready file size without a credential warning', () => {
    const warning = /Do not upload credentials/;
    const { rerender } = render(
      <AttachmentStrip entries={[entry({ key: '1', status: 'uploading' })]} onRemove={vi.fn()} />,
    );
    expect(screen.getByText('a.txt')).toBeInTheDocument();
    expect(screen.getByText('uploading…')).toBeInTheDocument();
    expect(screen.queryByText(warning)).not.toBeInTheDocument();
    rerender(<AttachmentStrip entries={[entry({ key: '1' })]} onRemove={vi.fn()} />);
    expect(screen.getByText('a.txt')).toBeInTheDocument();
    expect(screen.getByText('10 B')).toBeInTheDocument();
    expect(screen.queryByText('uploading…')).not.toBeInTheDocument();
    expect(screen.queryByText(warning)).not.toBeInTheDocument();
  });

  it('draws an image as a thumbnail above the text instead of a file chip', () => {
    render(
      <AttachmentStrip
        entries={[
          entry({ key: '1', path: 'pasted-image-a.png', previewUrl: 'blob:shot' }),
          entry({ key: '2', path: 'notes.md' }),
        ]}
        onRemove={vi.fn()}
      />,
    );
    const image = screen.getByRole('img', { name: 'pasted-image-a.png' });
    expect(image).toHaveAttribute('src', 'blob:shot');
    // The generated upload name is not repeated as a chip label.
    expect(screen.queryByText('pasted-image-a.png')).not.toBeInTheDocument();
    // An ordinary file keeps its chip, in the same list.
    expect(screen.getByText('notes.md')).toBeInTheDocument();
  });

  it('falls back to the file chip when the image cannot be decoded', () => {
    render(
      <AttachmentStrip
        entries={[entry({ key: '1', path: 'broken.png', previewUrl: 'blob:broken' })]}
        onRemove={vi.fn()}
      />,
    );
    fireEvent.error(screen.getByRole('img', { name: 'broken.png' }));
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    // The entry itself is untouched: it is still attached and still sends.
    expect(screen.getByText('broken.png')).toBeInTheDocument();
    expect(screen.getByText('10 B')).toBeInTheDocument();
  });

  it('drops an image back to a chip when its upload failed', () => {
    render(
      <AttachmentStrip
        entries={[
          entry({ key: '1', path: 'rejected.png', status: 'rejected', error: 'over 5 MB' }),
        ]}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(screen.getByText('over 5 MB')).toBeInTheDocument();
  });

  it('names each thumbnail removal button after its file', () => {
    const removed = vi.fn();
    render(
      <AttachmentStrip
        entries={[entry({ key: '1', path: 'shot.png', previewUrl: 'blob:shot' })]}
        onRemove={removed}
      />,
    );
    const button = screen.getByRole('button', { name: 'Remove shot.png' });
    // Reachable and operable from the keyboard.
    button.focus();
    expect(button).toHaveFocus();
    fireEvent.click(button);
    expect(removed).toHaveBeenCalledExactlyOnceWith('1');
  });

  it('shows upload progress over the thumbnail until the file lands', () => {
    const { rerender } = render(
      <AttachmentStrip
        entries={[entry({ key: '1', path: 'shot.png', status: 'uploading', previewUrl: 'blob:s' })]}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.getByText('uploading…')).toBeInTheDocument();
    rerender(
      <AttachmentStrip
        entries={[entry({ key: '1', path: 'shot.png', previewUrl: 'blob:s' })]}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.queryByText('uploading…')).not.toBeInTheDocument();
  });

  it('announces upload progress on a thumbnail, not only draws it', () => {
    render(
      <AttachmentStrip
        entries={[entry({ key: '1', path: 'shot.png', status: 'uploading', previewUrl: 'blob:s' })]}
        onRemove={vi.fn()}
      />,
    );
    expect(screen.getByText('uploading…').closest('[aria-hidden="true"]')).toBeNull();
  });

  it('keeps keyboard focus in the composer after a removal', () => {
    function Composer() {
      const [entries, setEntries] = useState([
        entry({ key: '1', path: 'one.png', previewUrl: 'blob:1' }),
        entry({ key: '2', path: 'notes.md' }),
        entry({ key: '3', path: 'three.png', previewUrl: 'blob:3' }),
      ]);
      return (
        <div>
          <AttachmentStrip
            entries={entries}
            onRemove={(key) => setEntries((old) => old.filter((row) => row.key !== key))}
          />
          <textarea aria-label="Message" />
        </div>
      );
    }
    render(<Composer />);
    const press = (name: string) => {
      const button = screen.getByRole('button', { name });
      button.focus();
      fireEvent.click(button);
    };

    // The item that takes the removed one's place gets focus...
    press('Remove notes.md');
    expect(screen.getByRole('button', { name: 'Remove three.png' })).toHaveFocus();
    // ...or the one before it, when the last was removed...
    press('Remove three.png');
    expect(screen.getByRole('button', { name: 'Remove one.png' })).toHaveFocus();
    // ...and the message box once nothing is attached.
    press('Remove one.png');
    expect(screen.getByRole('textbox', { name: 'Message' })).toHaveFocus();
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
