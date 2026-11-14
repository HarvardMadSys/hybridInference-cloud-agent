// @vitest-environment jsdom
// Sign-up's Terms dialog: read in place, agreed to only with Agree, and only
// after the end of the text was on screen; nothing is sent without it, from
// any submit path; and an answer belongs to the Terms it was given for.
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '@/lib/api/accounts';
import type { AuthOptions } from '@/lib/api/accounts';
import { SignupPage } from './SignupPage';
import { TermsConsent, useTermsConsent } from './TermsConsent';
import { shownTerms } from './termsContent';

vi.mock('next/navigation', () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock('@/lib/auth/sessionSync', () => ({ announceSession: vi.fn() }));
vi.mock('@/lib/api/accounts', () => ({
  getAuthOptions: vi.fn(),
  signupAccount: vi.fn(),
  signupPhone: vi.fn(),
  sendPhoneCode: vi.fn(),
}));

const TEXT =
  'Last updated: May 1, 2026.\n\n1. About. These are the terms.\n\n2. End. The last words.';
const OPTIONS: AuthOptions = {
  signup_enabled: true,
  phone_signup_enabled: true,
  email_delivery: true,
  require_email_verification: true,
  phone: { enabled: true, default_country_code: '86', allowed_prefixes: ['+86'] },
  turnstile_site_key: '',
  terms_url: '',
  terms_text: TEXT,
  setup_required: false,
  max_accounts_per_browser: 5,
};

/** IntersectionObserver as the browser gives it, driven by the test. */
const observed: { callback: IntersectionObserverCallback; targets: Element[] }[] = [];
class FakeIntersectionObserver {
  private record: (typeof observed)[number];
  constructor(callback: IntersectionObserverCallback) {
    this.record = { callback, targets: [] };
    observed.push(this.record);
  }
  observe(target: Element) {
    this.record.targets.push(target);
  }
  disconnect() {
    observed.splice(observed.indexOf(this.record), 1);
  }
  unobserve() {}
  takeRecords() {
    return [];
  }
}
/** Bring the end of the text on screen. */
function reachEnd() {
  act(() => {
    for (const { callback, targets } of [...observed])
      callback(
        targets.map((target) => ({ target, isIntersecting: true }) as IntersectionObserverEntry),
        {} as IntersectionObserver,
      );
  });
}

const dialog = () => screen.getByRole('dialog', { name: 'Terms of Service' });
const agreeButton = () => within(dialog()).getByRole('button', { name: 'Agree' });
const createButton = () => screen.getByRole('button', { name: 'Create account' });
function fill(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}
function fillEmail() {
  fill('Email', 'ada@example.edu');
  fill('Password', 'Goodpass123');
  fill('Confirm password', 'Goodpass123');
}
/**
 * The Terms dialog sign-up opens by itself, once it has finished opening. The
 * dialog is on screen a render before its effects focus its text, listen for
 * Escape and start watching for the end of the text, so wait for that focus.
 */
async function openedDialog() {
  const shown = await screen.findByRole('dialog', { name: 'Terms of Service' });
  const text = within(shown).getByRole('region', { name: 'Terms of Service text' });
  await waitFor(() => expect(text).toHaveFocus());
  return shown;
}

beforeEach(() => {
  vi.resetAllMocks();
  observed.length = 0;
  vi.stubGlobal('IntersectionObserver', FakeIntersectionObserver);
  vi.mocked(api.getAuthOptions).mockResolvedValue(OPTIONS);
  vi.mocked(api.signupAccount).mockResolvedValue({ ok: true });
  vi.mocked(api.signupPhone).mockResolvedValue({ ok: true, requires_approval: false });
  vi.mocked(api.sendPhoneCode).mockResolvedValue({ ok: true, retry_after: 0 });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  document.body.style.overflow = '';
});

describe('the Terms dialog on sign-up', () => {
  it('opens by itself, and offers Agree only once the end of the text is on screen', async () => {
    render(<SignupPage />);
    expect(await openedDialog()).toBeInTheDocument();
    const region = within(dialog()).getByRole('region', { name: 'Terms of Service text' });
    expect(region).toHaveFocus();
    expect(within(region).getByRole('heading', { name: '2.End.' })).toBeInTheDocument();
    expect(within(dialog()).getByText('Last updated: May 1, 2026.')).toBeInTheDocument();
    expect(agreeButton()).toBeDisabled();
    expect(within(dialog()).getByText('Read to the end of the Terms to agree.')).toBeVisible();
    // A disabled button is not the only gate: its handler checks too.
    fireEvent.click(agreeButton());
    expect(dialog()).toBeInTheDocument();

    reachEnd();

    expect(agreeButton()).toBeEnabled();
    expect(within(dialog()).getByText('You have reached the end of the Terms.')).toBeVisible();
    fireEvent.click(agreeButton());
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByText('You agreed to the Terms of Service.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Read again' })).toHaveFocus();
  });

  it('keeps what was typed through closing and reopening, and closing never agrees', async () => {
    render(<SignupPage />);
    await openedDialog();
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    const opener = screen.getByRole('button', { name: 'Read and agree' });
    expect(opener).toHaveFocus();
    expect(
      screen.getByText(
        'Read the Terms of Service and agree to them before you create your account.',
      ),
    ).toBeInTheDocument();
    fill('Name', 'Ada');
    fill('How will you use Cloud Agent?', 'Thesis');
    fillEmail();
    expect(createButton()).toBeDisabled();

    fireEvent.click(opener);
    reachEnd();
    // The backdrop and the close button close it too, without agreeing.
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Close' }));
    expect(createButton()).toBeDisabled();
    fireEvent.click(opener);
    // Having reached the end once is remembered for these Terms.
    fireEvent.click(agreeButton());

    expect(screen.getByLabelText('Name')).toHaveValue('Ada');
    expect(screen.getByLabelText('How will you use Cloud Agent?')).toHaveValue('Thesis');
    expect(screen.getByLabelText('Email')).toHaveValue('ada@example.edu');
    expect(screen.getByLabelText('Password')).toHaveValue('Goodpass123');
    expect(createButton()).toBeEnabled();
    fireEvent.click(createButton());
    await waitFor(() =>
      expect(api.signupAccount).toHaveBeenCalledWith('ada@example.edu', 'Goodpass123', {
        user_name: 'Ada',
        use_case: 'Thesis',
        accepted_tos: true,
        turnstile_token: undefined,
      }),
    );
  });

  it('withdraws the agreement on Disagree, and a direct email submit then sends nothing', async () => {
    render(<SignupPage />);
    await openedDialog();
    reachEnd();
    fireEvent.click(agreeButton());
    fillEmail();
    fireEvent.click(screen.getByRole('button', { name: 'Read again' }));
    fireEvent.click(within(dialog()).getByRole('button', { name: 'Disagree' }));
    expect(
      screen.getByText('You did not agree. An account cannot be created without agreeing.'),
    ).toBeInTheDocument();
    expect(createButton()).toBeDisabled();

    fireEvent.submit(createButton().closest('form')!);

    expect(api.signupAccount).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Agree to the Terms of Service before you create your account.',
    );
    // The way to agree opens right away.
    expect(dialog()).toBeInTheDocument();
  });

  it('sends no phone sign-up without the agreement or a full code, whatever submits', async () => {
    render(<SignupPage />);
    await openedDialog();
    fireEvent.keyDown(window, { key: 'Escape' });
    fireEvent.click(screen.getByRole('tab', { name: 'Phone number' }));
    fill('Phone number', '13800138000');
    fill('Verification code', '123456');
    fireEvent.submit(createButton().closest('form')!);
    expect(api.signupPhone).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Agree to the Terms of Service before you create your account.',
    );

    reachEnd();
    fireEvent.click(agreeButton());
    fill('Verification code', '123');
    fireEvent.submit(createButton().closest('form')!);
    expect(api.signupPhone).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Enter the six-digit code sent to your phone.',
    );

    fill('Verification code', '123456');
    fireEvent.submit(createButton().closest('form')!);
    await waitFor(() =>
      expect(api.signupPhone).toHaveBeenCalledWith(
        expect.objectContaining({ phone: '13800138000', code: '123456', accepted_tos: true }),
      ),
    );
  });

  it('sends no email sign-up without the check where Turnstile is on', async () => {
    window.turnstile = { render: () => 'widget-1', remove: () => undefined };
    vi.mocked(api.getAuthOptions).mockResolvedValue({ ...OPTIONS, turnstile_site_key: 'site' });
    render(<SignupPage />);
    await openedDialog();
    reachEnd();
    fireEvent.click(agreeButton());
    fillEmail();
    fireEvent.submit(createButton().closest('form')!);
    expect(api.signupAccount).not.toHaveBeenCalled();
    expect(screen.getByRole('alert')).toHaveTextContent(
      'Complete the check before you create your account.',
    );
    delete window.turnstile;
  });

  it('reads the end from the text box’s scroll where IntersectionObserver is missing', async () => {
    vi.stubGlobal('IntersectionObserver', undefined);
    // A text five screens long.
    const sizes = vi.spyOn(HTMLElement.prototype, 'scrollHeight', 'get').mockReturnValue(2000);
    vi.spyOn(HTMLElement.prototype, 'clientHeight', 'get').mockReturnValue(400);
    render(<SignupPage />);
    await openedDialog();
    const long = within(dialog()).getByRole('region', { name: 'Terms of Service text' });
    expect(agreeButton()).toBeDisabled();
    long.scrollTop = 900;
    fireEvent.scroll(long);
    expect(agreeButton()).toBeDisabled();
    long.scrollTop = 1600;
    fireEvent.scroll(long);
    expect(agreeButton()).toBeEnabled();
    sizes.mockRestore();
    vi.restoreAllMocks();
  });
});

describe('Terms other than text', () => {
  it('is honest about a link elsewhere: it opens a new tab and is not measured', async () => {
    vi.mocked(api.getAuthOptions).mockResolvedValue({
      ...OPTIONS,
      terms_text: '',
      terms_url: 'https://lab.example.edu/terms',
    });
    render(<SignupPage />);
    const shown = await screen.findByRole('dialog', { name: 'Terms of Service' });
    expect(within(shown).getByRole('link', { name: /Open the Terms of Service/ })).toHaveAttribute(
      'rel',
      'noopener noreferrer',
    );
    expect(
      within(shown).getByText(/cannot show that document or tell whether you have read it/),
    ).toBeInTheDocument();
    expect(shown.querySelector('iframe')).toBeNull();
    expect(agreeButton()).toBeEnabled();
  });

  it('opens nothing by itself when no Terms were published, and still asks for the agreement', async () => {
    vi.mocked(api.getAuthOptions).mockResolvedValue({ ...OPTIONS, terms_text: '', terms_url: '' });
    render(<SignupPage />);
    const opener = await screen.findByRole('button', { name: 'Read and agree' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(createButton()).toBeDisabled();
    fireEvent.click(opener);
    expect(within(dialog()).getByText(/has not published its terms here/)).toBeInTheDocument();
    fireEvent.click(agreeButton());
    expect(screen.getByText('You agreed to the Terms of Service.')).toBeInTheDocument();
  });

  it('says so when the sign-up options cannot be read', async () => {
    vi.mocked(api.getAuthOptions).mockRejectedValue(new Error('offline'));
    render(<SignupPage />);
    expect(await screen.findByText('Sign-up could not be loaded.')).toBeInTheDocument();
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});

describe('an agreement belongs to the Terms it was given for', () => {
  function Harness({ text }: { text: string }) {
    const consent = useTermsConsent(shownTerms({ terms_url: '', terms_text: text }, 'en'));
    return (
      <>
        <TermsConsent consent={consent} />
        <output data-testid="agreed">{String(consent.agreed)}</output>
      </>
    );
  }

  it('asks again, with the end unread, when the Terms change', () => {
    const { rerender } = render(<Harness text="Version one." />);
    reachEnd();
    fireEvent.click(agreeButton());
    expect(screen.getByTestId('agreed')).toHaveTextContent('true');
    // The same Terms again change nothing.
    rerender(<Harness text="Version one." />);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByTestId('agreed')).toHaveTextContent('true');

    rerender(<Harness text="Version two." />);

    expect(screen.getByTestId('agreed')).toHaveTextContent('false');
    expect(
      screen.getByText(
        'The Terms of Service changed after you agreed. Read them again to continue.',
      ),
    ).toBeInTheDocument();
    expect(within(dialog()).getByText('Version two.')).toBeInTheDocument();
    expect(agreeButton()).toBeDisabled();
    reachEnd();
    fireEvent.click(agreeButton());
    expect(screen.getByTestId('agreed')).toHaveTextContent('true');
  });
});
