import { describe, expect, it, vi } from 'vitest';
import { fetchWithAuth } from './client';
import { announceProfileSaved } from './profileEvents';
import { saveUserProfile, type UserProfile } from './userProfile';

vi.mock('./client', async (original) => ({
  ...(await original<typeof import('./client')>()),
  fetchWithAuth: vi.fn(),
}));
vi.mock('./profileEvents', () => ({ announceProfileSaved: vi.fn() }));

const saved: UserProfile = {
  email: 'alice@example.com',
  first_name: 'Alice',
  last_name: '',
  image_data_url: null,
  revision: 2,
  companion_data_url: 'data:image/svg+xml,<svg/>',
};

describe('profile save notifications', () => {
  it.each([false, true])(
    'announces a save only while its request is active (aborted: %s)',
    async (aborted) => {
      vi.clearAllMocks();
      const controller = new AbortController();
      let resolve!: (response: Response) => void;
      vi.mocked(fetchWithAuth).mockReturnValueOnce(
        new Promise((done) => {
          resolve = done;
        }),
      );
      const request = saveUserProfile(
        { first_name: 'Alice', last_name: '', expected_revision: 1 },
        controller.signal,
      );
      if (aborted) controller.abort();
      resolve(new Response(JSON.stringify(saved), { status: 200 }));
      expect(await request).toEqual(saved);
      if (aborted) expect(announceProfileSaved).not.toHaveBeenCalled();
      else expect(announceProfileSaved).toHaveBeenCalledExactlyOnceWith(saved);
    },
  );
});
