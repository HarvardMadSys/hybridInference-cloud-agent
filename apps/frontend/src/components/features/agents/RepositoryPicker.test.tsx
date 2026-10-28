// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RepositoryPicker } from './RepositoryPicker';

vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return { useT: () => (key: Parameters<typeof translate>[1]) => translate('en', key) };
});

afterEach(cleanup);

function renderPicker(overrides: Partial<Parameters<typeof RepositoryPicker>[0]> = {}) {
  const onChange = vi.fn();
  const onOpenChange = vi.fn();
  const onRefresh = vi.fn();
  const onConfigure = vi.fn();
  render(
    <RepositoryPicker
      repos={['zeta/repo', 'alpha/one', 'alpha/one']}
      value=""
      onChange={onChange}
      open
      onOpenChange={onOpenChange}
      onRefresh={onRefresh}
      onConfigure={onConfigure}
      {...overrides}
    />,
  );
  return { onChange, onOpenChange, onRefresh, onConfigure };
}

describe('RepositoryPicker', () => {
  it('sorts and deduplicates repositories, searches, and keeps its footer actions', () => {
    const { onChange, onRefresh, onConfigure } = renderPicker();
    const panel = screen.getByRole('dialog', { name: 'Repository' });
    const options = [...panel.querySelectorAll('[data-repository-option]')].map((row) =>
      row.getAttribute('aria-label'),
    );
    expect(options).toEqual(['Start from scratch', 'alpha/one', 'zeta/repo']);

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search repositories…' }), {
      target: { value: 'ZETA' },
    });
    expect(panel.querySelectorAll('[data-repository-option]')).toHaveLength(1);
    expect(screen.getByRole('button', { name: 'zeta/repo' })).toBeInTheDocument();

    fireEvent.change(screen.getByRole('searchbox', { name: 'Search repositories…' }), {
      target: { value: 'missing' },
    });
    expect(panel.querySelectorAll('[data-repository-option]')).toHaveLength(0);
    expect(onChange).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: /Configure repository access/ }));
    expect(onConfigure).toHaveBeenCalledTimes(1);
  });

  it('keeps Enter in the search as the first match and Escape with the trigger', () => {
    const { onChange, onOpenChange } = renderPicker();
    const search = screen.getByRole('searchbox', { name: 'Search repositories…' });
    fireEvent.change(search, { target: { value: 'alpha' } });
    fireEvent.keyDown(search, { key: 'Enter' });
    expect(onChange).toHaveBeenCalledWith('alpha/one');
    expect(onOpenChange).toHaveBeenCalledWith(false);

    fireEvent.keyDown(screen.getByRole('dialog', { name: 'Repository' }), { key: 'Escape' });
    expect(onOpenChange).toHaveBeenLastCalledWith(false);
  });

  it('offers the temporary workspace and connects when GitHub is disconnected', () => {
    const onConnect = vi.fn();
    renderPicker({
      disconnected: true,
      onConnect,
      onRefresh: undefined,
      onConfigure: undefined,
    });
    expect(screen.getByRole('button', { name: 'Start from scratch' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Connect GitHub to use a repository' }));
    expect(onConnect).toHaveBeenCalledTimes(1);
  });
});
