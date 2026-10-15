// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { getInsights, type WorkInsights } from '@/lib/api/insights';
import { getUserProfile, type UserProfile } from '@/lib/api/userProfile';
import { announceProfileSaved } from '@/lib/api/profileEvents';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { UserAvatar } from './UserAvatar';

const auth = vi.hoisted(() => ({ userId: 'alice' as string | undefined }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: auth.userId ? { id: auth.userId } : null } }),
}));
vi.mock('@/lib/api/insights', () => ({ getInsights: vi.fn() }));
vi.mock('@/lib/api/userProfile', () => ({ getUserProfile: vi.fn() }));

const profile: UserProfile = {
  email: 'alice@example.com',
  first_name: 'Alice',
  last_name: '',
  image_data_url: null,
  revision: 1,
  companion_data_url: 'data:image/svg+xml,<svg id="fixed"></svg>',
};
const photo = 'data:image/png;base64,cGhvdG8=';

const view: WorkInsights = {
  avatar: { data_url: 'data:image/svg+xml,<svg></svg>', style: 'illustrated' },
  focus_areas: [],
  suggestions: [],
};

function avatar() {
  return (
    <LocaleProvider>
      <UserAvatar />
    </LocaleProvider>
  );
}

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  auth.userId = 'alice';
  vi.mocked(getInsights).mockResolvedValue(view);
  vi.mocked(getUserProfile).mockResolvedValue(profile);
});
afterEach(cleanup);

describe('homepage avatar', () => {
  it('shows the cached avatar immediately on return while both reads refresh', async () => {
    const first = render(avatar());
    expect(await screen.findByRole('img')).toHaveAttribute('src', view.avatar.data_url);
    first.unmount();
    let resolveInsights!: (next: WorkInsights) => void;
    let resolveProfile!: (next: UserProfile) => void;
    vi.mocked(getInsights).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveInsights = resolve;
      }),
    );
    vi.mocked(getUserProfile).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveProfile = resolve;
      }),
    );
    render(avatar());
    expect(screen.getByRole('img')).toHaveAttribute('src', view.avatar.data_url);
    expect(getInsights).toHaveBeenCalledTimes(2);
    expect(getUserProfile).toHaveBeenCalledTimes(2);
    const updated = {
      ...view,
      avatar: { ...view.avatar, data_url: 'data:image/svg+xml,<svg id="new"></svg>' },
    };
    await act(async () => resolveInsights(updated));
    expect(screen.getByRole('img')).toHaveAttribute('src', updated.avatar.data_url);
    await act(async () => resolveProfile({ ...profile, image_data_url: photo }));
    expect(screen.getByRole('img', { name: 'Your profile image' })).toHaveAttribute('src', photo);
  });

  it('keeps the cached photo if background profile and insights reads fail', async () => {
    vi.mocked(getUserProfile).mockResolvedValueOnce({ ...profile, image_data_url: photo });
    const first = render(avatar());
    expect(await screen.findByRole('img')).toHaveAttribute('src', photo);
    first.unmount();
    vi.mocked(getUserProfile).mockRejectedValueOnce(new Error('Offline'));
    vi.mocked(getInsights).mockRejectedValueOnce(new Error('Offline'));
    render(avatar());
    expect(screen.getByRole('img')).toHaveAttribute('src', photo);
    await act(async () => {});
    expect(screen.getByRole('img')).toHaveAttribute('src', photo);
  });

  it('shares pending reads and keeps them alive until the last avatar unmounts', async () => {
    let resolveInsights!: (next: WorkInsights) => void;
    let resolveProfile!: (next: UserProfile) => void;
    vi.mocked(getInsights).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveInsights = resolve;
      }),
    );
    vi.mocked(getUserProfile).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveProfile = resolve;
      }),
    );
    const first = render(avatar());
    const second = render(avatar());
    expect(getInsights).toHaveBeenCalledTimes(1);
    expect(getUserProfile).toHaveBeenCalledTimes(1);
    const insightsSignal = vi.mocked(getInsights).mock.calls[0][0]!;
    const profileSignal = vi.mocked(getUserProfile).mock.calls[0][0]!;
    first.unmount();
    expect(insightsSignal.aborted).toBe(false);
    expect(profileSignal.aborted).toBe(false);
    await act(async () => {
      resolveInsights(view);
      resolveProfile(profile);
    });
    expect(screen.getByRole('img')).toHaveAttribute('src', view.avatar.data_url);
    second.unmount();
  });

  it('caches saved photos immediately and rejects an older background response', async () => {
    const first = render(avatar());
    await screen.findByRole('img');
    first.unmount();
    let resolveOld!: (next: UserProfile) => void;
    vi.mocked(getUserProfile).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveOld = resolve;
      }),
    );
    const second = render(avatar());
    const signal = vi.mocked(getUserProfile).mock.calls[1][0]!;
    act(() => announceProfileSaved({ ...profile, image_data_url: photo, revision: 2 }));
    expect(signal.aborted).toBe(true);
    expect(screen.getByRole('img')).toHaveAttribute('src', photo);
    await act(async () => resolveOld(profile));
    expect(screen.getByRole('img')).toHaveAttribute('src', photo);
    second.unmount();
    vi.mocked(getUserProfile).mockReturnValueOnce(new Promise(() => {}));
    render(avatar());
    expect(screen.getByRole('img')).toHaveAttribute('src', photo);
  });

  it('prefers the uploaded photo even when the work profile cannot load', async () => {
    vi.mocked(getUserProfile).mockResolvedValue({ ...profile, image_data_url: photo });
    vi.mocked(getInsights).mockRejectedValue(new Error('Offline'));
    render(avatar());
    const image = await screen.findByRole('img', { name: 'Your profile image' });
    expect(image).toHaveAttribute('src', photo);
    expect(image).toHaveClass('object-cover');
    expect(
      screen.queryByRole('img', { name: 'Your Pokémon companion avatar' }),
    ).not.toBeInTheDocument();
  });

  it('uses the supplied work profile without fetching it again', async () => {
    render(<UserAvatar view={view} />);
    expect(await screen.findByRole('img')).toHaveAttribute('src', view.avatar.data_url);
    expect(getInsights).not.toHaveBeenCalled();
    expect(getUserProfile).toHaveBeenCalledTimes(1);
  });

  it('waits for the photo lookup before showing the companion', async () => {
    let resolve!: (next: UserProfile) => void;
    vi.mocked(getUserProfile).mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    render(avatar());
    await act(async () => {});
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    await act(async () => resolve({ ...profile, image_data_url: photo }));
    expect(screen.getByRole('img')).toHaveAttribute('src', photo);
  });

  it('uses the companion if the photo lookup fails', async () => {
    vi.mocked(getUserProfile).mockRejectedValueOnce(new Error('Offline'));
    render(avatar());
    expect(await screen.findByRole('img')).toHaveAttribute('src', view.avatar.data_url);
  });

  it('restores the companion on the next homepage visit after a photo is removed', async () => {
    vi.mocked(getUserProfile).mockResolvedValueOnce({ ...profile, image_data_url: photo });
    const rendered = render(avatar());
    expect(await screen.findByRole('img')).toHaveAttribute('src', photo);
    rendered.unmount();
    render(avatar());
    expect(await screen.findByRole('img')).toHaveAttribute('src', view.avatar.data_url);
  });

  it('clears another account photo immediately and aborts its pending lookup', async () => {
    vi.mocked(getUserProfile).mockResolvedValueOnce({ ...profile, image_data_url: photo });
    const rendered = render(avatar());
    expect(await screen.findByRole('img')).toHaveAttribute('src', photo);
    let resolveBob!: (next: UserProfile) => void;
    vi.mocked(getUserProfile).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveBob = resolve;
      }),
    );
    auth.userId = 'bob';
    rendered.rerender(avatar());
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    const signal = vi.mocked(getUserProfile).mock.calls[1][0]!;
    auth.userId = 'carol';
    rendered.rerender(avatar());
    expect(signal.aborted).toBe(true);
    expect(await screen.findByRole('img')).toHaveAttribute('src', view.avatar.data_url);
    await act(async () => resolveBob({ ...profile, image_data_url: photo }));
    expect(screen.getByRole('img')).toHaveAttribute('src', view.avatar.data_url);
  });
  it('loads the latest work theme each time the homepage opens', async () => {
    for (const index of [0, 1, 2]) {
      const dataUrl = `data:image/svg+xml,<svg id="theme-${index}"></svg>`;
      vi.mocked(getInsights).mockResolvedValueOnce({
        ...view,
        avatar: { ...view.avatar, data_url: dataUrl },
      });
      const rendered = render(avatar());
      const image = await screen.findByRole('img', {
        name: 'Your Pokémon companion avatar',
      });
      expect(image).toHaveAttribute('src', dataUrl);
      expect(image).toHaveAttribute(
        'title',
        'Your Pokémon stays the same while its habitat and orbiting badges reflect your recent work.',
      );
      rendered.unmount();
    }
    expect(getInsights).toHaveBeenCalledTimes(3);
  });

  it('removes the previous avatar immediately on an account switch and ignores stale responses', async () => {
    const rendered = render(avatar());
    await screen.findByRole('img');
    let resolveBob!: (next: WorkInsights) => void;
    vi.mocked(getInsights).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveBob = resolve;
      }),
    );
    auth.userId = 'bob';
    rendered.rerender(avatar());
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    const bobSignal = vi.mocked(getInsights).mock.calls[1][0]!;
    const carol = {
      ...view,
      avatar: { ...view.avatar, data_url: 'data:image/svg+xml,<svg id="carol"></svg>' },
    };
    vi.mocked(getInsights).mockResolvedValueOnce(carol);
    auth.userId = 'carol';
    rendered.rerender(avatar());
    expect(bobSignal.aborted).toBe(true);
    expect(await screen.findByRole('img')).toHaveAttribute('src', carol.avatar.data_url);
    await act(async () => resolveBob(view));
    expect(screen.getByRole('img')).toHaveAttribute('src', carol.avatar.data_url);
  });

  it('keeps the fallback on load failure and does not fetch while signed out', async () => {
    vi.mocked(getInsights).mockRejectedValueOnce(new Error('Offline'));
    const rendered = render(avatar());
    await act(async () => {});
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(rendered.container.querySelector('svg')).toBeInTheDocument();
    auth.userId = undefined;
    rendered.rerender(avatar());
    expect(getInsights).toHaveBeenCalledTimes(1);
    expect(getUserProfile).toHaveBeenCalledTimes(1);
  });

  it('aborts an in-flight request when unmounted', () => {
    vi.mocked(getInsights).mockReturnValueOnce(new Promise(() => {}));
    const rendered = render(avatar());
    const signal = vi.mocked(getInsights).mock.calls[0][0]!;
    const profileSignal = vi.mocked(getUserProfile).mock.calls[0][0]!;
    rendered.unmount();
    expect(signal.aborted).toBe(true);
    expect(profileSignal.aborted).toBe(true);
  });

  it('translates the avatar description into Chinese', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(avatar());
    expect(await screen.findByRole('img', { name: '你的宝可梦伙伴头像' })).toHaveAttribute(
      'title',
      '宝可梦伙伴保持不变，栖息地与环绕徽章会随近期工作变化。',
    );
  });
});
