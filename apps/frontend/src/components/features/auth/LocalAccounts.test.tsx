// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import * as api from '@/lib/api/accounts';
import { APIError } from '@/lib/utils/errors';
import { SignupForm } from './SignupForm';
import { MailTokenForm } from './MailTokenForm';
import { EmailRequestForm } from './EmailRequestForm';
import { LocalLogin } from './LocalLogin';
import { LocalAccountGate } from './LocalAccountGate';

vi.mock('next/navigation', () => ({ useSearchParams: () => new URLSearchParams() }));
vi.mock('@/lib/api/accounts', () => ({
  signupAccount: vi.fn(),
  loginAccount: vi.fn(),
  getAuthConfig: vi.fn(),
  verifyAccountEmail: vi.fn(),
  resetPassword: vi.fn(),
  forgotPassword: vi.fn(),
  resendVerification: vi.fn(),
}));
vi.mock('@/lib/auth/sessionSync', () => ({ announceSession: vi.fn() }));
function fill(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}
beforeEach(() => {
  vi.resetAllMocks();
  window.history.replaceState(null, '', '/');
});
afterEach(cleanup);

describe('independent account forms', () => {
  it('rejects a weak password and a mismatched confirmation before sending credentials', async () => {
    render(<SignupForm />);
    fill('Email', 'user@example.test');
    fill('Password', 'short');
    fill('Confirm password', 'different');
    fireEvent.submit(screen.getByRole('button', { name: 'Create account' }).closest('form')!);
    expect(screen.getByRole('alert')).toHaveTextContent('8–1024');
    expect(api.signupAccount).not.toHaveBeenCalled();
    fill('Password', 'Goodpass123');
    fireEvent.submit(screen.getByRole('button', { name: 'Create account' }).closest('form')!);
    expect(screen.getByRole('alert')).toHaveTextContent('do not match');
    expect(api.signupAccount).not.toHaveBeenCalled();
  });
  it('sends only the email/password and does not imply automatic approval', async () => {
    vi.mocked(api.signupAccount).mockResolvedValue({ ok: true });
    render(<SignupForm />);
    fill('Email', 'user@example.test');
    fill('Password', 'Goodpass123');
    fill('Confirm password', 'Goodpass123');
    fireEvent.submit(screen.getByRole('button', { name: 'Create account' }).closest('form')!);
    await screen.findByText('Check your email');
    expect(api.signupAccount).toHaveBeenCalledWith('user@example.test', 'Goodpass123');
    expect(screen.getByRole('status')).toHaveTextContent('sign in to submit your application');
  });
  it('holds duplicate submissions while the first request is pending', () => {
    vi.mocked(api.signupAccount).mockReturnValue(new Promise(() => {}));
    render(<SignupForm />);
    fill('Email', 'user@example.test');
    fill('Password', 'Goodpass123');
    fill('Confirm password', 'Goodpass123');
    const form = screen.getByRole('button', { name: 'Create account' }).closest('form')!;
    fireEvent.submit(form);
    fireEvent.submit(form);
    expect(api.signupAccount).toHaveBeenCalledTimes(1);
  });
  it('verification consumes a fragment only after an explicit action and clears it from the address', async () => {
    vi.mocked(api.verifyAccountEmail).mockResolvedValue({ ok: true });
    window.history.replaceState(null, '', '/verify-email#token=mail-secret');
    render(<MailTokenForm purpose="verify" />);
    await waitFor(() => expect(window.location.hash).toBe(''));
    expect(api.verifyAccountEmail).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain('mail-secret');
    fireEvent.click(screen.getByRole('button', { name: 'Verify email' }));
    await screen.findByText('Email verified. Sign in to apply for Cloud Agent access.');
    expect(api.verifyAccountEmail).toHaveBeenCalledWith('mail-secret');
  });
  it('expired tokens offer recovery without pretending verification succeeded', async () => {
    vi.mocked(api.verifyAccountEmail).mockRejectedValue(
      new APIError('invalid_token', 'This link has expired.', 400),
    );
    window.history.replaceState(null, '', '/verify-email#token=old');
    render(<MailTokenForm purpose="verify" />);
    fireEvent.click(await screen.findByRole('button', { name: 'Verify email' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('expired');
    expect(screen.getByRole('link', { name: 'Request a new link' })).toHaveAttribute(
      'href',
      '/resend-verification',
    );
  });
  it('does not accept a token from a query parameter or storage', () => {
    window.history.replaceState(null, '', '/verify-email?token=unsafe-query');
    render(<MailTokenForm purpose="verify" />);
    expect(screen.getByRole('alert')).toHaveTextContent('incomplete');
    expect(screen.queryByRole('button', { name: 'Verify email' })).toBeNull();
    expect(api.verifyAccountEmail).not.toHaveBeenCalled();
  });
  it('resets the password without retaining the token or password in browser storage', async () => {
    vi.mocked(api.resetPassword).mockResolvedValue({ ok: true });
    window.history.replaceState(null, '', '/reset-password#token=reset-secret');
    render(<MailTokenForm purpose="reset" />);
    fill('Password', 'Goodpass123');
    fill('Confirm password', 'Goodpass123');
    fireEvent.submit(screen.getByRole('button', { name: 'Reset password' }).closest('form')!);
    await screen.findByText(/Password updated/);
    expect(api.resetPassword).toHaveBeenCalledWith('reset-secret', 'Goodpass123');
    expect(window.location.href).not.toContain('reset-secret');
    expect(JSON.stringify({ ...localStorage, ...sessionStorage })).not.toContain('reset-secret');
  });
  it('tells the holder what a reclaiming reset cleared and to apply again (review T2)', async () => {
    vi.mocked(api.resetPassword).mockResolvedValue({ ok: true, reclaimed: true });
    window.history.replaceState(null, '', '/reset-password#token=reclaim-secret');
    render(<MailTokenForm purpose="reset" />);
    fill('Password', 'Goodpass123');
    fill('Confirm password', 'Goodpass123');
    fireEvent.submit(screen.getByRole('button', { name: 'Reset password' }).closest('form')!);
    await screen.findByText(/Password updated/);
    expect(screen.getByText(/apply again from the waiting list/)).toBeInTheDocument();
  });
  it('recovery gives the same generic response without enumerating accounts', async () => {
    vi.mocked(api.forgotPassword).mockResolvedValue({ ok: true });
    render(<EmailRequestForm purpose="reset" />);
    fill('Email', 'unknown@example.test');
    fireEvent.submit(screen.getByRole('button', { name: 'Send email' }).closest('form')!);
    expect(await screen.findByRole('status')).toHaveTextContent('If this account is eligible');
  });
  it('offers verification only for a typed unverified response', async () => {
    vi.mocked(api.loginAccount).mockRejectedValue(
      new APIError('email_not_verified', 'Verify your email.', 403),
    );
    render(<LocalLogin signupEnabled />);
    fill('Email', 'user@example.test');
    fill('Password', 'Goodpass123');
    fireEvent.submit(screen.getByRole('button', { name: 'Sign in' }).closest('form')!);
    await screen.findByRole('alert');
    expect(screen.getByRole('link', { name: 'Resend verification email' })).toHaveAttribute(
      'href',
      '/resend-verification',
    );
    expect(screen.queryByText(/identity provider/)).toBeNull();
  });
  it('does not show a local signup form in Gateway mode', async () => {
    vi.mocked(api.getAuthConfig).mockResolvedValue({ mode: 'gateway', signup_enabled: false });
    render(
      <LocalAccountGate signup>
        <SignupForm />
      </LocalAccountGate>,
    );
    await screen.findByText('Account registration unavailable');
    expect(screen.queryByLabelText('Password')).toBeNull();
  });
});
