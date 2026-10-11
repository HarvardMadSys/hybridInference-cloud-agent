// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import {
  act,
  cleanup,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
} from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';
import {
  NOTIFICATION_PERMISSION_EVENT,
  notificationPreferencesStorageKey,
  useNotificationPreferences,
} from '@/components/features/agents/notificationPreferences';
import { NotificationSettings } from './NotificationSettings';

const auth = vi.hoisted(() => ({ userId: 'notification-settings-user' as string | undefined }));
vi.mock('@/components/providers/AuthProvider', () => ({
  useAuth: () => ({ state: { user: auth.userId ? { id: auth.userId } : null } }),
}));

let permission: NotificationPermission;
let requestPermission: ReturnType<typeof vi.fn<() => Promise<NotificationPermission>>>;
let accountNumber = 0;

function renderSettings() {
  return render(
    <LocaleProvider>
      <NotificationSettings />
    </LocaleProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
  permission = 'default';
  auth.userId = `notification-settings-${++accountNumber}`;
  requestPermission = vi.fn(async () => {
    permission = 'granted';
    return permission;
  });
  const notification = Object.assign(vi.fn(), { requestPermission });
  Object.defineProperty(notification, 'permission', { get: () => permission });
  vi.stubGlobal('Notification', notification);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('notification settings', () => {
  it('requests permission only from Enable and applies categories immediately', async () => {
    renderSettings();
    const monitor = renderHook(() => useNotificationPreferences(auth.userId));
    expect(requestPermission).not.toHaveBeenCalled();
    expect(screen.getByText(/Keep the app open/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('checkbox', { name: 'Agent questions' }));
    expect(monitor.result.current[0].questions).toBe(false);
    expect(requestPermission).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Enable notifications' }));

    await screen.findByRole('button', { name: 'Disable notifications' });
    expect(requestPermission).toHaveBeenCalledOnce();
    expect(monitor.result.current[0].enabled).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Disable notifications' }));
    expect(monitor.result.current[0].enabled).toBe(false);
  });

  it('does not enable notifications when permission is denied or dismissed', async () => {
    requestPermission.mockResolvedValueOnce('denied');
    renderSettings();
    const monitor = renderHook(() => useNotificationPreferences(auth.userId));
    fireEvent.click(screen.getByRole('button', { name: 'Enable notifications' }));
    expect(await screen.findByText(/Notifications are blocked/)).toBeInTheDocument();
    expect(monitor.result.current[0].enabled).toBe(false);
    expect(screen.getByRole('button', { name: 'Enable notifications' })).toBeDisabled();
  });

  it('offers to restore permission when saved notifications are enabled but browser permission resets', async () => {
    const permissionChanged = vi.fn();
    window.addEventListener(NOTIFICATION_PERMISSION_EVENT, permissionChanged, { once: true });
    localStorage.setItem(
      notificationPreferencesStorageKey(auth.userId!),
      JSON.stringify({ enabled: true, questions: true, turns: true }),
    );
    renderSettings();
    expect(screen.getByText('Browser notifications are off.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disable notifications' })).toBeEnabled();
    expect(requestPermission).not.toHaveBeenCalled();
    expect(permissionChanged).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Enable notifications' }));

    await screen.findByText('Browser notifications are enabled for this account.');
    expect(requestPermission).toHaveBeenCalledOnce();
    expect(permissionChanged).toHaveBeenCalledOnce();
    expect(screen.queryByRole('button', { name: 'Enable notifications' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Disable notifications' })).toBeEnabled();
  });

  it('leaves a dismissed permission prompt disabled with an available retry', async () => {
    requestPermission.mockResolvedValueOnce('default');
    renderSettings();
    const monitor = renderHook(() => useNotificationPreferences(auth.userId));
    fireEvent.click(screen.getByRole('button', { name: 'Enable notifications' }));
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Enable notifications' })).toBeEnabled(),
    );
    expect(monitor.result.current[0].enabled).toBe(false);
  });

  it('explains unsupported browsers and permission request failures', async () => {
    vi.stubGlobal('Notification', undefined);
    const first = renderSettings();
    expect(screen.getByText(/This browser cannot send notifications/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Enable notifications' })).toBeDisabled();
    first.unmount();
    vi.stubGlobal(
      'Notification',
      Object.assign(vi.fn(), {
        permission: 'default',
        requestPermission: vi.fn().mockRejectedValue(new Error('Browser blocked prompt')),
      }),
    );
    renderSettings();
    fireEvent.click(screen.getByRole('button', { name: 'Enable notifications' }));
    expect(await screen.findByText(/Could not enable notifications/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Enable notifications' })).toBeEnabled();
  });

  it('ignores a permission response after the active account changes', async () => {
    let finish!: (value: NotificationPermission) => void;
    requestPermission.mockImplementation(() => new Promise((resolve) => (finish = resolve)));
    const view = renderSettings();
    const originalUser = auth.userId!;
    fireEvent.click(screen.getByRole('button', { name: 'Enable notifications' }));
    auth.userId = 'different-notification-user';
    view.rerender(
      <LocaleProvider>
        <NotificationSettings />
      </LocaleProvider>,
    );
    await act(async () => finish('granted'));
    const otherAccount = renderHook(() => useNotificationPreferences(auth.userId));
    const originalAccount = renderHook(() => useNotificationPreferences(originalUser));
    expect(otherAccount.result.current[0].enabled).toBe(false);
    expect(originalAccount.result.current[0].enabled).toBe(false);
  });

  it('translates notification controls and permission guidance', async () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    permission = 'denied';
    vi.stubGlobal('Notification', Object.assign(vi.fn(), { permission, requestPermission }));
    renderSettings();
    expect(await screen.findByText(/通知已被阻止/)).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Agent 提问' })).toBeChecked();
  });
});
