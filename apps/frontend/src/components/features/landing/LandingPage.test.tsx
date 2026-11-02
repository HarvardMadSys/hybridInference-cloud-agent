// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { getAuthOptions, type AuthOptions } from '@/lib/api/accounts';
import { LandingPage } from './LandingPage';
import { EXAMPLES, STAGES, stageDuration } from './demoScript';
import { HOLD_MS } from './useDemoPlayer';

vi.mock('@/lib/api/accounts', async (original) => ({
  ...(await original<typeof import('@/lib/api/accounts')>()),
  getAuthOptions: vi.fn(),
}));

function options(changes: Partial<AuthOptions> = {}): AuthOptions {
  return {
    signup_enabled: true,
    phone_signup_enabled: false,
    email_delivery: true,
    require_email_verification: true,
    phone: { enabled: false, default_country_code: '', allowed_prefixes: [] },
    turnstile_site_key: '',
    terms_url: '',
    terms_text: '',
    setup_required: false,
    max_accounts_per_browser: 5,
    ...changes,
  };
}

const demo = () => document.querySelector('[role="img"]') as HTMLElement;
const row = () => demo().querySelector('[data-demo="pr-row"]');
const message = () => demo().querySelector('[data-demo="message"]');

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  localStorage.clear();
  vi.mocked(getAuthOptions).mockReset().mockResolvedValue(options());
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('the signed-out home page', () => {
  it('says what the product is and offers sign-up while the deployment takes sign-ups', async () => {
    render(<LandingPage />);
    await advance(0);

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('Your agents.At work.');
    const nav = screen.getByRole('navigation', { name: 'Account' });
    expect(within(nav).getByRole('link', { name: 'Sign in' })).toHaveAttribute('href', '/login');
    expect(within(nav).getByRole('link', { name: 'Sign up' })).toHaveAttribute('href', '/signup');
    const agents = screen.getByRole('region', { name: 'Supported agents' });
    for (const name of ['Claude Code', 'Codex', 'pi', 'OpenCode', 'Kilo Code'])
      expect(within(agents).getByText(name)).toBeInTheDocument();
  });

  it('offers only sign-in when sign-up is closed', async () => {
    vi.mocked(getAuthOptions).mockResolvedValue(options({ signup_enabled: false }));
    render(<LandingPage />);
    await advance(0);

    expect(screen.queryByRole('link', { name: 'Sign up' })).not.toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'Sign in' }).length).toBeGreaterThan(0);
  });

  it('stays usable when the account options cannot be read', async () => {
    vi.mocked(getAuthOptions).mockRejectedValue(new Error('offline'));
    render(<LandingPage />);
    await advance(0);

    expect(screen.queryByRole('link', { name: 'Sign up' })).not.toBeInTheDocument();
    expect(screen.getAllByRole('link', { name: 'Sign in' })[0]).toHaveAttribute('href', '/login');
  });

  it('reads in Chinese for a visitor who chose it', async () => {
    localStorage.setItem('agents.locale', 'zh-CN');
    render(
      <LocaleProvider>
        <LandingPage />
      </LocaleProvider>,
    );
    await advance(0);

    expect(screen.getByRole('heading', { level: 1 })).toHaveTextContent('你的 Agent，在云端开工。');
    expect(screen.getAllByRole('link', { name: '注册' })[0]).toHaveAttribute('href', '/signup');
  });

  it('plays a task through to an opened draft pull request, then moves to the next example', async () => {
    render(<LandingPage />);
    await advance(0);
    const first = EXAMPLES[0];

    expect(message()).toHaveAttribute('data-in', 'false');
    expect(screen.getByRole('button', { name: 'Add tests' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );

    // The opening delay, then the task is asked and sent.
    await advance(1100 + stageDuration(first, 0) + 300);
    expect(message()).toHaveAttribute('data-in', 'true');
    expect(message()).toHaveTextContent(first.prompt);

    await advance(stageDuration(first, 1) + stageDuration(first, 2));
    expect(row()).toHaveAttribute('data-state', 'opened');
    expect(screen.getByRole('button', { name: /Review the result/ })).toHaveAttribute(
      'aria-current',
      'step',
    );

    // After the pause, the next example starts on its own.
    await advance(HOLD_MS + 1000);
    expect(screen.getByRole('button', { name: 'Fix a bug' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(message()).toHaveAttribute('data-in', 'false');
  });

  it('holds the finished task while the pointer rests on it', async () => {
    render(<LandingPage />);
    await advance(0);
    const total = STAGES.reduce<number>((sum, stage) => sum + stageDuration(EXAMPLES[0], stage), 0);
    await advance(1100 + total + 200);
    expect(row()).toHaveAttribute('data-state', 'opened');

    fireEvent.pointerEnter(demo(), { pointerType: 'mouse' });
    await advance(HOLD_MS * 3);
    expect(screen.getByRole('button', { name: 'Add tests' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );

    fireEvent.pointerLeave(demo());
    await advance(HOLD_MS + 1000);
    expect(screen.getByRole('button', { name: 'Fix a bug' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('replays one stage when its step is chosen, and stops there', async () => {
    render(<LandingPage />);
    await advance(2000);

    fireEvent.click(screen.getByRole('button', { name: /Review the result/ }));
    await advance(50);
    // The jump lands on the end of the previous stage: the work is done, no PR yet.
    expect(row()).toHaveAttribute('data-state', 'hidden');

    await advance(stageDuration(EXAMPLES[0], 2));
    expect(row()).toHaveAttribute('data-state', 'opened');

    // Chosen by hand, it does not move on by itself.
    await advance(HOLD_MS * 2);
    expect(row()).toHaveAttribute('data-state', 'opened');
    expect(screen.getByRole('button', { name: 'Add tests' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
  });

  it('switches to the chosen example', async () => {
    render(<LandingPage />);
    await advance(2000);

    fireEvent.click(screen.getByRole('button', { name: 'Refactor' }));
    await advance(1000);
    expect(screen.getByRole('button', { name: 'Refactor' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await advance(stageDuration(EXAMPLES[2], 0) + 500);
    expect(message()).toHaveTextContent(EXAMPLES[2].prompt);
  });
});
