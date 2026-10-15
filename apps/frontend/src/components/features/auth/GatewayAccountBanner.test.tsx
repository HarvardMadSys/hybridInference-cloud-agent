// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GatewayAccountBanner } from './GatewayAccountBanner';

let userId: string | undefined = 'agent-user';

vi.mock('next/navigation', () => ({ usePathname: () => '/ajob_123' }));

vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return {
    useAuth: () => ({ state: { user: userId ? { id: userId } : null } }),
    useT: () => (key: Parameters<typeof translate>[1]) => translate('en', key),
  };
});

function visit(search: string) {
  window.history.replaceState(null, '', `/ajob_123${search}`);
}

beforeEach(() => {
  sessionStorage.clear();
  userId = 'agent-user';
  visit('');
});

afterEach(cleanup);

describe('GatewayAccountBanner', () => {
  it('offers to switch when the console sent this tab as another account', () => {
    visit('?gateway_account=console-user&tab=output');
    render(<GatewayAccountBanner />);

    expect(screen.getByRole('status')).toHaveTextContent(
      'You opened Agents from a different account',
    );
    const url = new URL(
      screen.getByRole('link', { name: 'Switch to that account' }).getAttribute('href') ?? '',
      'https://agents.example',
    );
    expect(url.searchParams.get('prompt')).toBe('select_account');
    expect(url.searchParams.get('login_hint')).toBe('console-user');
    expect(url.searchParams.get('return_to')).toBe('/ajob_123');
  });

  it('takes the account off the address, keeping the rest of it', () => {
    visit('?gateway_account=console-user&tab=output');
    render(<GatewayAccountBanner />);

    expect(window.location.search).toBe('?tab=output');
  });

  it('stays across pages of the same tab until dismissed', () => {
    visit('?gateway_account=console-user');
    render(<GatewayAccountBanner />);
    cleanup();
    visit('');
    render(<GatewayAccountBanner />);
    expect(screen.getByRole('status')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Keep this account' }));

    expect(screen.queryByRole('status')).toBeNull();
    cleanup();
    render(<GatewayAccountBanner />);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('says nothing when both use the same account', () => {
    visit('?gateway_account=agent-user');
    render(<GatewayAccountBanner />);

    expect(screen.queryByRole('status')).toBeNull();
    expect(sessionStorage.length).toBe(0);
  });

  it('says nothing without a link from the console', () => {
    render(<GatewayAccountBanner />);

    expect(screen.queryByRole('status')).toBeNull();
  });

  it('ignores an account id it would not pass to the gateway', () => {
    visit(`?gateway_account=${encodeURIComponent('<script>')}`);
    render(<GatewayAccountBanner />);

    expect(screen.queryByRole('status')).toBeNull();
  });
});
