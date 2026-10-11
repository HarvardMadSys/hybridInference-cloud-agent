// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const parseMarkdown = vi.hoisted(() => vi.fn());
vi.mock('react-markdown', async (importOriginal) => {
  const original = await importOriginal<typeof import('react-markdown')>();
  return {
    ...original,
    default: (props: Parameters<typeof original.default>[0]) => {
      parseMarkdown();
      return original.default(props);
    },
  };
});

import { Markdown, WorkspaceLinkProvider } from './Markdown';

afterEach(cleanup);
beforeEach(() => parseMarkdown.mockClear());

describe('Markdown rendering work', () => {
  it('parses settled messages once while the surrounding composer updates', () => {
    function Conversation({ draft }: { draft: string }) {
      return (
        <>
          {Array.from({ length: 20 }, (_, index) => (
            <Markdown key={index} text={`Reply ${index}: **complete**`} />
          ))}
          <textarea aria-label="Draft" value={draft} readOnly />
        </>
      );
    }
    const { rerender } = render(<Conversation draft="" />);
    expect(parseMarkdown).toHaveBeenCalledTimes(20);
    for (const draft of ['h', 'he', 'hel', 'hell', 'hello']) {
      rerender(<Conversation draft={draft} />);
    }
    expect(screen.getByRole('textbox', { name: 'Draft' })).toHaveValue('hello');
    expect(screen.getAllByText('complete')).toHaveLength(20);
    expect(parseMarkdown).toHaveBeenCalledTimes(20);
  });

  it('renders new streamed text immediately', () => {
    const { rerender } = render(<Markdown text="First token" />);
    rerender(<Markdown text="First token and **more**" />);
    expect(screen.getByText('more').tagName).toBe('STRONG');
    expect(parseMarkdown).toHaveBeenCalledTimes(2);
  });

  it('uses the current workspace action without reparsing unchanged text', () => {
    const first = vi.fn();
    const latest = vi.fn();
    const text = '[File](file:///workspace/src/app.ts)';
    const { rerender } = render(
      <WorkspaceLinkProvider onOpenFile={first}>
        <Markdown text={text} />
      </WorkspaceLinkProvider>,
    );
    rerender(
      <WorkspaceLinkProvider onOpenFile={latest}>
        <Markdown text={text} />
      </WorkspaceLinkProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'File' }));
    expect(first).not.toHaveBeenCalled();
    expect(latest).toHaveBeenCalledWith('src/app.ts');
    expect(parseMarkdown).toHaveBeenCalledTimes(1);

    rerender(<Markdown text={text} />);
    expect(screen.queryByRole('button', { name: 'File' })).toBeNull();
    expect(screen.getByText('File').closest('a')).toHaveAttribute('href', '');
    expect(parseMarkdown).toHaveBeenCalledTimes(2);
  });
});
