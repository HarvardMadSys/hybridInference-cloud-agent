// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { act, cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { getUserMemory, type UserMemoryView } from '@/lib/api/userMemory';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import { UserAvatar } from './UserAvatar';

const auth = vi.hoisted(() => ({ userId: 'alice' as string | undefined }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: auth.userId ? { id: auth.userId } : null } }),
}));
vi.mock('@/lib/api/userMemory', () => ({ getUserMemory: vi.fn() }));

const view: UserMemoryView = {
  enabled: true,
  revision: 1,
  updated_at: null,
  avatar: { data_url: 'data:image/svg+xml,<svg></svg>', style: 'illustrated' },
  portrait: {
    summary: '',
    projects: [],
    focus_areas: [],
    preferred_runtime: null,
    preferred_model: null,
  },
  memories: [],
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
  vi.mocked(getUserMemory).mockResolvedValue(view);
});
afterEach(cleanup);

describe('homepage avatar', () => {
  it('loads the latest work theme each time the homepage opens, including paused and cleared themes', async () => {
    for (const [index, enabled] of [true, false, false].entries()) {
      const dataUrl = `data:image/svg+xml,<svg id="theme-${index}"></svg>`;
      vi.mocked(getUserMemory).mockResolvedValueOnce({
        ...view,
        enabled,
        avatar: { ...view.avatar, data_url: dataUrl },
      });
      const rendered = render(avatar());
      const image = await screen.findByRole('img', {
        name: 'Your automatically generated illustrated avatar',
      });
      expect(image).toHaveAttribute('src', dataUrl);
      expect(image).toHaveAttribute(
        'title',
        'Your base character stays the same while colors and details reflect your recent work.',
      );
      rendered.unmount();
    }
    expect(getUserMemory).toHaveBeenCalledTimes(3);
  });

  it('removes the previous avatar immediately on an account switch and ignores stale responses', async () => {
    const rendered = render(avatar());
    await screen.findByRole('img');
    let resolveBob!: (next: UserMemoryView) => void;
    vi.mocked(getUserMemory).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveBob = resolve;
      }),
    );
    auth.userId = 'bob';
    rendered.rerender(avatar());
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    const bobSignal = vi.mocked(getUserMemory).mock.calls[1][0]!;
    const carol = {
      ...view,
      avatar: { ...view.avatar, data_url: 'data:image/svg+xml,<svg id="carol"></svg>' },
    };
    vi.mocked(getUserMemory).mockResolvedValueOnce(carol);
    auth.userId = 'carol';
    rendered.rerender(avatar());
    expect(bobSignal.aborted).toBe(true);
    expect(await screen.findByRole('img')).toHaveAttribute('src', carol.avatar.data_url);
    await act(async () => resolveBob(view));
    expect(screen.getByRole('img')).toHaveAttribute('src', carol.avatar.data_url);
  });

  it('keeps the fallback on load failure and does not fetch while signed out', async () => {
    vi.mocked(getUserMemory).mockRejectedValueOnce(new Error('Offline'));
    const rendered = render(avatar());
    await act(async () => {});
    expect(screen.queryByRole('img')).not.toBeInTheDocument();
    expect(rendered.container.querySelector('svg')).toBeInTheDocument();
    auth.userId = undefined;
    rendered.rerender(avatar());
    expect(getUserMemory).toHaveBeenCalledTimes(1);
  });

  it('aborts an in-flight request when unmounted', () => {
    vi.mocked(getUserMemory).mockReturnValueOnce(new Promise(() => {}));
    const rendered = render(avatar());
    const signal = vi.mocked(getUserMemory).mock.calls[0][0]!;
    rendered.unmount();
    expect(signal.aborted).toBe(true);
  });

  it('translates the avatar description into Chinese', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    render(avatar());
    expect(await screen.findByRole('img', { name: '为你自动生成的插画头像' })).toHaveAttribute(
      'title',
      '头像的基础角色保持不变，配色与细节会随近期工作变化。',
    );
  });
});
