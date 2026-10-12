// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Markdown, WorkspaceLinkProvider } from './Markdown';

afterEach(cleanup);

describe('workspace links in a conversation', () => {
  it('opens file references in the workspace and leaves web links external', () => {
    const openFile = vi.fn();
    render(
      <WorkspaceLinkProvider onOpenFile={openFile}>
        <Markdown
          text={'[App](/workspace/src/app.ts:12) [License](LICENSE) [Web](https://example.org)'}
        />
      </WorkspaceLinkProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'App' }));
    fireEvent.click(screen.getByRole('button', { name: 'License' }));
    expect(openFile.mock.calls).toEqual([['src/app.ts'], ['LICENSE']]);
    expect(screen.getByRole('link', { name: 'Web' }).getAttribute('target')).toBe('_blank');
  });

  it('supports sandbox file URLs without widening allowed external URL schemes', () => {
    const openFile = vi.fn();
    const { container } = render(
      <WorkspaceLinkProvider onOpenFile={openFile}>
        <Markdown text={'[File](file:///workspace/readme.md) [Danger](javascript:alert(1))'} />
      </WorkspaceLinkProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'File' }));
    expect(openFile).toHaveBeenCalledWith('readme.md');
    expect(container.querySelector('a')?.getAttribute('href')).toBe('');
  });

  it('keeps file-looking links unchanged outside the conversation provider', () => {
    render(<Markdown text={'[File](docs/readme.md)'} />);
    expect(screen.getByRole('link', { name: 'File' }).getAttribute('href')).toBe('docs/readme.md');
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('preserves code and rejects traversal and raw HTML as workspace actions', () => {
    const openFile = vi.fn();
    const { container } = render(
      <WorkspaceLinkProvider onOpenFile={openFile}>
        <Markdown text={'`src/app.ts` [Bad](../private.txt)\n\n<img src=x onerror="alert(1)">'} />
      </WorkspaceLinkProvider>,
    );
    expect(screen.getByText('src/app.ts').tagName).toBe('CODE');
    expect(screen.queryByRole('button')).toBeNull();
    expect(container.querySelector('img')).toBeNull();
    expect(openFile).not.toHaveBeenCalled();
  });
});
