// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { APIError } from '@/lib/utils/errors';
import { getUserProfile, saveUserProfile, type UserProfile } from '@/lib/api/userProfile';
import { ProfileSettings } from './ProfileSettings';

const auth = vi.hoisted(() => ({ user: { id: 'alice' } as { id: string } | null }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: auth.user } }),
}));
vi.mock('@/lib/api/userProfile', () => ({ getUserProfile: vi.fn(), saveUserProfile: vi.fn() }));

const profile: UserProfile = {
  email: 'alice@example.com',
  first_name: 'Alice',
  last_name: 'Yang',
  image_data_url: null,
  revision: 3,
};

beforeEach(() => {
  auth.user = { id: 'alice' };
  vi.mocked(getUserProfile).mockResolvedValue(profile);
  vi.mocked(saveUserProfile).mockImplementation(async (value) => ({
    ...profile,
    ...value,
    revision: 4,
  }));
});
afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

async function openProfile() {
  const view = render(<ProfileSettings />);
  await screen.findByDisplayValue('Alice');
  return view;
}

describe('Profile settings', () => {
  it('shows read-only email and persists editable names with the current revision', async () => {
    await openProfile();
    expect(screen.getByText('alice@example.com')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Email' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    fireEvent.change(screen.getByLabelText('First Name'), { target: { value: ' 沐昕 ' } });
    fireEvent.change(screen.getByLabelText('Last Name'), { target: { value: '田' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Profile saved.');
    expect(saveUserProfile).toHaveBeenCalledWith(
      { first_name: '沐昕', last_name: '田', expected_revision: 3 },
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('previews uploads and saves them only after Save is pressed', async () => {
    await openProfile();
    const file = new File(['image bytes'], 'profile.png', { type: 'image/png' });
    fireEvent.change(screen.getByLabelText('Upload image'), { target: { files: [file] } });
    const image = await screen.findByRole('img', { name: 'Your profile image' });
    expect(image).toHaveAttribute('src', 'data:image/png;base64,aW1hZ2UgYnl0ZXM=');
    expect(saveUserProfile).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Profile saved.');
    expect(saveUserProfile).toHaveBeenCalledWith(
      expect.objectContaining({ image_data_url: image.getAttribute('src') }),
      expect.any(AbortSignal),
    );
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('removes an uploaded picture with an explicit null update', async () => {
    vi.mocked(getUserProfile).mockResolvedValue({
      ...profile,
      image_data_url: 'data:image/png;base64,eA==',
    });
    await openProfile();
    fireEvent.click(screen.getByRole('button', { name: 'Remove image' }));
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Profile saved.');
    expect(saveUserProfile).toHaveBeenCalledWith(
      expect.objectContaining({ image_data_url: null }),
      expect.any(AbortSignal),
    );
  });

  it.each([
    [
      new File(['<svg/>'], 'image.svg', { type: 'image/svg+xml' }),
      'Choose a PNG, JPEG, or WebP image.',
    ],
    [
      new File([new Uint8Array(2 * 1024 * 1024 + 1)], 'large.png', { type: 'image/png' }),
      'Choose a nonempty image up to 2 MB.',
    ],
  ])('rejects invalid files without changing the saved profile', async (file, message) => {
    await openProfile();
    fireEvent.change(screen.getByLabelText('Upload image'), { target: { files: [file] } });
    expect(await screen.findByRole('alert')).toHaveTextContent(message);
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(saveUserProfile).not.toHaveBeenCalled();
  });

  it('keeps unsaved changes on failure and can retry', async () => {
    vi.mocked(saveUserProfile).mockRejectedValueOnce(new Error('offline'));
    await openProfile();
    fireEvent.change(screen.getByLabelText('First Name'), { target: { value: 'Draft' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Could not save your profile. Your edits are kept. Please try again.');
    expect(screen.getByLabelText('First Name')).toHaveValue('Draft');
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText('Profile saved.');
    expect(saveUserProfile).toHaveBeenCalledTimes(2);
  });

  it('requires reviewing a newer profile after a conflict', async () => {
    vi.mocked(saveUserProfile).mockRejectedValueOnce(new APIError('conflict', 'changed', 409));
    await openProfile();
    fireEvent.change(screen.getByLabelText('Last Name'), { target: { value: 'Draft' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await screen.findByText(/Your profile changed elsewhere/);
    expect(screen.getByLabelText('Last Name')).toHaveValue('Draft');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
    vi.mocked(getUserProfile).mockResolvedValue({ ...profile, last_name: 'Updated', revision: 4 });
    fireEvent.click(screen.getByRole('button', { name: 'Reload profile' }));
    await screen.findByDisplayValue('Updated');
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('retries initial loading and keeps the form disabled until it succeeds', async () => {
    vi.mocked(getUserProfile).mockRejectedValueOnce(new Error('offline'));
    render(<ProfileSettings />);
    await screen.findByText('Could not load your profile. Please try again.');
    expect(screen.getByLabelText('First Name')).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await screen.findByDisplayValue('Alice');
    expect(screen.getByLabelText('First Name')).toBeEnabled();
  });

  it('clears drafts and aborts requests when the account changes', async () => {
    let resolveOld: ((value: UserProfile) => void) | undefined;
    vi.mocked(saveUserProfile).mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveOld = resolve;
        }),
    );
    const view = await openProfile();
    fireEvent.change(screen.getByLabelText('First Name'), { target: { value: 'Private draft' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    const signal = vi.mocked(saveUserProfile).mock.calls[0][1];
    auth.user = { id: 'bob' };
    vi.mocked(getUserProfile).mockResolvedValue({
      ...profile,
      email: 'bob@example.com',
      first_name: 'Bob',
    });
    view.rerender(<ProfileSettings />);
    await screen.findByDisplayValue('Bob');
    expect(signal?.aborted).toBe(true);
    await act(async () => resolveOld?.({ ...profile, first_name: 'Private draft' }));
    expect(screen.getByLabelText('First Name')).toHaveValue('Bob');
    expect(screen.queryByText('alice@example.com')).not.toBeInTheDocument();
  });
});
