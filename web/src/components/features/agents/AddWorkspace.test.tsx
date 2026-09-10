// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { getAgentConfig, type AgentConfigApi } from '@/lib/api/agents';
import { AddWorkspace } from './AddWorkspace';
vi.mock('@/lib/api/agents', () => ({ getAgentConfig: vi.fn() }));
vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return { useT: () => (key: Parameters<typeof translate>[1]) => translate('en', key) };
});
const config = (repos = ['owner/repo', 'owner/empty']): AgentConfigApi => ({
  repos,
  github_connected: true,
  github_install_url: null,
  models: [],
  runtimes: [],
  setup_egress_tier: null,
  agent_egress_tier: null,
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});
describe('add workspace picker', () => {
  it('lazily searches accessible repositories, deduplicates them and returns focus on selection', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config(['owner/repo', 'owner/empty', 'owner/empty']),
    );
    const select = vi.fn();
    render(<AddWorkspace userId="alice" existing={['owner/repo']} onSelect={select} />);
    expect(getAgentConfig).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Add workspace' }));
    await screen.findByRole('button', { name: 'owner/repo · Already added' });
    expect(screen.getAllByRole('button', { name: 'owner/empty' })).toHaveLength(1);
    const input = screen.getByRole('textbox', { name: 'Search repositories…' });
    expect(input).toHaveFocus();
    fireEvent.change(input, { target: { value: 'EMPTY' } });
    expect(screen.queryByTitle('owner/repo')).not.toBeInTheDocument();
    fireEvent.keyDown(input, { key: 'ArrowDown' });
    expect(screen.getByRole('button', { name: 'owner/empty' })).toHaveFocus();
    fireEvent.click(screen.getByRole('button', { name: 'owner/empty' }));
    expect(select).toHaveBeenCalledExactlyOnceWith('owner/empty');
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add workspace' })).toHaveFocus();
  });
  it('does not accept an arbitrary repository and closes on Escape or outside click', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config());
    const select = vi.fn();
    render(<AddWorkspace userId="alice" existing={[]} onSelect={select} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add workspace' }));
    await screen.findByTitle('owner/repo');
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'unauthorized/private' } });
    expect(screen.getByText('No matching repositories.')).toBeInTheDocument();
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(select).not.toHaveBeenCalled();
    fireEvent.keyDown(input, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Add workspace' }));
    await screen.findByTitle('owner/repo');
    fireEvent.pointerDown(document.body);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });
  it('retries errors and directs disconnected accounts to repository integrations', async () => {
    vi.mocked(getAgentConfig)
      .mockRejectedValueOnce(new Error('Unavailable'))
      .mockResolvedValueOnce({ ...config(), github_connected: false });
    render(<AddWorkspace userId="alice" existing={[]} onSelect={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add workspace' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Unavailable');
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(await screen.findByRole('link', { name: 'Manage repository access' })).toHaveAttribute(
      'href',
      '/integrations',
    );
    expect(screen.queryByTitle('owner/repo')).not.toBeInTheDocument();
  });
  it("hides another owner's pending response and disables anonymous additions", async () => {
    let finish!: (value: AgentConfigApi) => void;
    vi.mocked(getAgentConfig)
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = resolve;
          }),
      )
      .mockResolvedValueOnce(config(['bob/repo']));
    const { rerender } = render(<AddWorkspace userId="alice" existing={[]} onSelect={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: 'Add workspace' }));
    rerender(<AddWorkspace userId="bob" existing={[]} onSelect={vi.fn()} />);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    await act(async () => finish(config(['alice/private'])));
    fireEvent.click(screen.getByRole('button', { name: 'Add workspace' }));
    await screen.findByTitle('bob/repo');
    expect(screen.queryByTitle('alice/private')).not.toBeInTheDocument();
    rerender(<AddWorkspace existing={[]} onSelect={vi.fn()} />);
    expect(screen.getByRole('button', { name: 'Add workspace' })).toBeDisabled();
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});
