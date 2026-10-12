// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import SetupPage from './page';

vi.mock('../(agents)/admin/settings/ApplicationSettings', () => ({
  default: ({
    recoveryToken,
    onAccessDenied,
  }: {
    recoveryToken: string;
    onAccessDenied: () => void;
  }) => (
    <div>
      <span>Settings editor unlocked</span>
      <span data-testid="has-token">{Boolean(recoveryToken).toString()}</span>
      <button onClick={onAccessDenied}>Simulate access denial</button>
    </div>
  ),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('administrator recovery setup', () => {
  it('requires a token, clears its input after unlocking, and removes access when locked', () => {
    const store = vi.spyOn(Storage.prototype, 'setItem');
    render(<SetupPage />);
    expect(screen.queryByText('Settings editor unlocked')).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText('Administrator recovery token'), {
      target: { value: 'test-private-recovery-token' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Unlock setup' }));
    expect(screen.getByText('Settings editor unlocked')).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent('test-private-recovery-token');
    expect(screen.queryByLabelText('Administrator recovery token')).not.toBeInTheDocument();
    expect(store).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Lock setup' }));
    expect(screen.getByLabelText('Administrator recovery token')).toHaveValue('');
    expect(screen.queryByText('Settings editor unlocked')).not.toBeInTheDocument();
  });

  it('drops recovery access when the administrator token is rejected', () => {
    render(<SetupPage />);
    fireEvent.change(screen.getByLabelText('Administrator recovery token'), {
      target: { value: 'invalid-token' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Unlock setup' }));
    fireEvent.click(screen.getByRole('button', { name: 'Simulate access denial' }));
    expect(screen.queryByText('Settings editor unlocked')).not.toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent('The recovery token was not accepted.');
    expect(screen.getByLabelText('Administrator recovery token')).toHaveValue('');
  });
});
