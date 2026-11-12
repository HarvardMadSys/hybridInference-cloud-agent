// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { getAgentConfig, type AgentConfigApi } from '@/lib/api/agents';
import { providedSource, TEST_RESETS_AT } from '@/test/connectionConfig';

import { resetTime } from '../agents/composer/providedModels';
import { ProvidedModelsCard } from './ProvidedModelsCard';

vi.mock('@/lib/api/agents', () => ({ getAgentConfig: vi.fn() }));

function config(overrides: Partial<AgentConfigApi> = {}): AgentConfigApi {
  return {
    repos: [],
    runtimes: ['codex'],
    github_connected: true,
    github_install_url: null,
    model_connections_enabled: true,
    ...overrides,
  };
}

function cells(row: HTMLElement): string[] {
  return within(row)
    .getAllByRole('cell')
    .map((cell) => cell.textContent ?? '');
}

beforeEach(() => vi.mocked(getAgentConfig).mockReset());
afterEach(cleanup);

describe('the provided models card in Settings', () => {
  it('lists each model with its allowance, today’s use and what is left', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        provided_sources: [
          providedSource({
            'house-fast': [1500, 1458],
            'house-open': [null, 2300],
            'house-spent': [20, 20],
          }),
        ],
      }),
    );
    render(<ProvidedModelsCard />);

    const card = await screen.findByRole('region', { name: 'Provided by this deployment' });
    const source = within(card).getByRole('article', { name: 'House models' });
    expect(within(source).getByText('New tasks start here')).toBeInTheDocument();
    expect(
      within(source)
        .getAllByRole('columnheader')
        .map((header) => header.textContent),
    ).toEqual(['Model', 'Daily allowance', 'Used today', 'Left today']);
    const time = resetTime(TEST_RESETS_AT);
    expect(cells(within(source).getByRole('row', { name: /^house-fast/ }))).toEqual([
      '1,500',
      '1,458',
      '42',
    ]);
    expect(cells(within(source).getByRole('row', { name: /^house-open/ }))).toEqual([
      'Unlimited',
      '2,300',
      'Unlimited',
    ]);
    expect(cells(within(source).getByRole('row', { name: /^house-spent/ }))).toEqual([
      '20',
      '20',
      `None until ${time}`,
    ]);
    // The reset is said in the viewer's own time.
    expect(
      within(source).getByText(`Counts are per UTC day and start over at ${time}, your time.`),
    ).toBeInTheDocument();
  });

  it('has nothing to edit', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({ provided_sources: [providedSource({ 'house-fast': [100, 1] })] }),
    );
    render(<ProvidedModelsCard />);
    const card = await screen.findByRole('region', { name: 'Provided by this deployment' });
    for (const role of ['button', 'textbox', 'combobox', 'checkbox', 'link']) {
      expect(within(card).queryByRole(role)).not.toBeInTheDocument();
    }
  });

  it('shows every provided source, and says when one offers this account no model', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        provided_sources: [
          providedSource({ 'house-fast': [100, 1] }),
          providedSource(
            {},
            { id: 'oconn_ffffffffffffffffffffffffffffffff', name: 'Reserved', is_default: false },
          ),
        ],
      }),
    );
    render(<ProvidedModelsCard />);
    const reserved = await screen.findByRole('article', { name: 'Reserved' });
    expect(reserved).toHaveTextContent('No model on this connection is offered to your account.');
    expect(within(reserved).queryByText('New tasks start here')).not.toBeInTheDocument();
    expect(screen.getByRole('article', { name: 'House models' })).toBeInTheDocument();
  });

  it('is not shown when the deployment provides nothing', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(config({ provided_sources: [] }));
    const { container } = render(<ProvidedModelsCard />);
    await waitFor(() => expect(getAgentConfig).toHaveBeenCalledTimes(1));
    await Promise.resolve();
    expect(container).toBeEmptyDOMElement();
  });

  it('is not shown on a control plane that predates provided models, or when it cannot be read', async () => {
    vi.mocked(getAgentConfig).mockResolvedValueOnce(config());
    const first = render(<ProvidedModelsCard />);
    await waitFor(() => expect(getAgentConfig).toHaveBeenCalledTimes(1));
    await Promise.resolve();
    expect(first.container).toBeEmptyDOMElement();
    first.unmount();

    vi.mocked(getAgentConfig).mockRejectedValueOnce(new Error('offline'));
    const second = render(<ProvidedModelsCard />);
    await waitFor(() => expect(getAgentConfig).toHaveBeenCalledTimes(2));
    await Promise.resolve();
    expect(second.container).toBeEmptyDOMElement();
  });

  it('is not shown where model connections are switched off', async () => {
    vi.mocked(getAgentConfig).mockResolvedValue(
      config({
        model_connections_enabled: false,
        provided_sources: [providedSource({ 'house-fast': [100, 1] })],
      }),
    );
    const { container } = render(<ProvidedModelsCard />);
    await waitFor(() => expect(getAgentConfig).toHaveBeenCalledTimes(1));
    await Promise.resolve();
    expect(container).toBeEmptyDOMElement();
  });
});
