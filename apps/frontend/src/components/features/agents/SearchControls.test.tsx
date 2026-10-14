// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { createRef, useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return {
    useT: () => (key: Parameters<typeof translate>[1], vars?: Parameters<typeof translate>[2]) =>
      translate('en', key, vars),
  };
});
import { SearchControls } from './SearchControls';
afterEach(cleanup);
function Controls({ activeRepo = 'owner/repo' }: { activeRepo?: string | null }) {
  const [query, setQuery] = useState('');
  const [scope, setScope] = useState<'all' | 'current'>('all');
  const [includeArchived, setIncludeArchived] = useState(false);
  return (
    <SearchControls
      inputRef={null}
      query={query}
      onQueryChange={setQuery}
      scope={scope}
      onScopeChange={setScope}
      activeRepo={activeRepo}
      includeArchived={includeArchived}
      onIncludeArchivedChange={setIncludeArchived}
      onClose={() => {}}
      onInputKeyDown={() => {}}
    />
  );
}
describe('compact sidebar search controls', () => {
  it('offers project and archive toggle buttons without a native select or checkbox', () => {
    render(<Controls />);
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    const scope = screen.getByRole('button', { name: 'Search scope' });
    expect(scope).toHaveTextContent('All projects');
    expect(scope).toHaveAttribute('aria-pressed', 'false');
    expect(scope).toHaveAttribute('title', 'Search only owner/repo');
    fireEvent.click(scope);
    expect(scope).toHaveTextContent('Current project');
    expect(scope).toHaveAttribute('aria-pressed', 'true');
    expect(scope).toHaveAttribute('title', 'Search all projects');
    fireEvent.click(scope);
    expect(scope).toHaveTextContent('All projects');
    const archived = screen.getByRole('button', { name: 'Include archived' });
    expect(archived).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(archived);
    expect(archived).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(archived);
    expect(archived).toHaveAttribute('aria-pressed', 'false');
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'query' } });
    expect(screen.getByRole('textbox')).toHaveValue('query');
  });
  it('names a temporary workspace as people see it, not by its internal key', () => {
    render(<Controls activeRepo=":temporary" />);
    const title = screen.getByRole('button', { name: 'Search scope' }).getAttribute('title');
    expect(title).not.toContain(':temporary');
    expect(title).toContain('Start from scratch');
  });
  it('explains unavailable current-project scope while preserving archive search', () => {
    render(<Controls activeRepo={null} />);
    const scope = screen.getByRole('button', { name: 'Search scope' });
    expect(scope).toBeDisabled();
    expect(scope).toHaveAttribute('title', 'Open a project to limit search to it');
    fireEvent.click(scope);
    expect(scope).toHaveAttribute('aria-pressed', 'false');
    expect(screen.getByRole('button', { name: 'Include archived' })).toBeEnabled();
  });
  it('preserves input focus control and result-navigation keys, and closes from the inline button or Escape', () => {
    const inputRef = createRef<HTMLInputElement>();
    const key = vi.fn();
    const close = vi.fn();
    render(
      <SearchControls
        inputRef={inputRef}
        query="query"
        onQueryChange={() => {}}
        scope="all"
        onScopeChange={() => {}}
        activeRepo="owner/repo"
        includeArchived={false}
        onIncludeArchivedChange={() => {}}
        onClose={close}
        onInputKeyDown={key}
      />,
    );
    inputRef.current!.focus();
    expect(screen.getByRole('textbox')).toHaveFocus();
    fireEvent.keyDown(inputRef.current!, { key: 'ArrowDown' });
    fireEvent.keyDown(inputRef.current!, { key: 'Enter' });
    expect(key.mock.calls.map(([event]) => event.key)).toEqual(['ArrowDown', 'Enter']);
    expect(close).not.toHaveBeenCalled();
    fireEvent.keyDown(screen.getByRole('button', { name: 'Include archived' }), { key: 'Escape' });
    expect(close).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: 'Close search' }));
    expect(close).toHaveBeenCalledTimes(2);
  });
});
