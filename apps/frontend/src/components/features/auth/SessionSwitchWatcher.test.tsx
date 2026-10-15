// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { act, cleanup, render } from '@testing-library/react';
import toast from 'react-hot-toast';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { SESSION_SWITCHED_EVENT } from '@/lib/auth/sessionSync';
import { SessionSwitchWatcher } from './SessionSwitchWatcher';

const router = vi.hoisted(() => ({ replace: vi.fn() }));

vi.mock('next/navigation', () => ({ useRouter: () => router }));
vi.mock('react-hot-toast', () => ({ default: vi.fn() }));
vi.mock('@/components/providers', async () => {
  const { translate } = await import('@/lib/i18n/translate');
  return {
    useT: () => (key: Parameters<typeof translate>[1]) => translate('en', key),
  };
});

afterEach(cleanup);

describe('SessionSwitchWatcher', () => {
  it('returns to the start page and says why when another tab switched accounts', () => {
    render(<SessionSwitchWatcher />);

    act(() => {
      window.dispatchEvent(new CustomEvent(SESSION_SWITCHED_EVENT, { detail: 'second' }));
    });

    expect(router.replace).toHaveBeenCalledWith('/');
    expect(toast).toHaveBeenCalledWith(
      'Agents switched to another account in a different tab, so this tab went back to the start.',
    );
  });
});
