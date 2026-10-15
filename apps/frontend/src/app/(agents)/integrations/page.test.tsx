// @vitest-environment jsdom
import { cleanup, render } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import IntegrationsPage from './page';
const navigation = vi.hoisted(() => ({ search: '', replace: vi.fn() }));
vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(navigation.search),
  useRouter: () => ({ replace: navigation.replace }),
}));
beforeEach(() => {
  navigation.replace.mockClear();
  navigation.search = '';
  window.history.replaceState(null, '', '/integrations');
});
afterEach(cleanup);
it('opens personal settings for a saved generic settings link', () => {
  render(<IntegrationsPage />);
  expect(navigation.replace).toHaveBeenCalledWith('/settings/my-settings');
});
it.each([
  ['mcp_authorized=0&mcp_connection=3&mcp_message=access_denied', '/settings/plugins'],
  ['mcp_authorized=1&mcp_connection=3', '/settings/plugins'],
  ['connected=github', '/settings/integrations'],
  ['connected=gitlab', '/settings/integrations'],
])('preserves authorization callback parameters: %s', (search, page) => {
  navigation.search = search;
  render(<IntegrationsPage />);
  expect(navigation.replace).toHaveBeenCalledWith(`${page}?${search}`);
});
it.each([
  ['#settings-environments', '/settings/environments'],
  ['#settings-user-memory', '/settings/my-settings'],
  ['#settings-skills', '/settings/plugins'],
  ['#settings-getting-started', '/settings/getting-started'],
])('preserves the legacy bookmark %s', (hash, page) => {
  window.history.replaceState(null, '', `/integrations${hash}`);
  render(<IntegrationsPage />);
  expect(navigation.replace).toHaveBeenCalledWith(page + hash);
});
