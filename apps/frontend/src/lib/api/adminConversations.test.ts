import { afterEach, expect, it, vi } from 'vitest';

import {
  conversationHref,
  getAdminConversation,
  getAdminConversations,
} from './adminConversations';
import * as client from './client';

afterEach(() => vi.restoreAllMocks());

it('uses admin-only, uncached reads and preserves encoded filter and cursor identities', async () => {
  const fetch = vi
    .spyOn(client, 'fetchWithAuth')
    .mockImplementation(
      async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
    );
  const controller = new AbortController();
  await getAdminConversations(
    { user_id: 'owner&one', repo: 'org/repo', model: 'model+a', cursor: 'page+2' },
    controller.signal,
  );
  const url = new URL(fetch.mock.calls[0][1], 'http://test');
  expect(url.pathname).toBe('/admin/agent/conversations');
  expect(Object.fromEntries(url.searchParams)).toEqual({
    user_id: 'owner&one',
    repo: 'org/repo',
    model: 'model+a',
    cursor: 'page+2',
  });
  await getAdminConversation('thread/one', { cursor: 'older+2' }, controller.signal);
  expect(fetch).toHaveBeenLastCalledWith(
    expect.any(String),
    '/admin/agent/conversations/thread%2Fone?cursor=older%2B2',
    { signal: controller.signal, cache: 'no-store' },
  );
  expect(
    fetch.mock.calls.every(
      ([, , init]) => init?.cache === 'no-store' && init?.signal === controller.signal,
    ),
  ).toBe(true);
});

it('carries selection into a session without reusing the list pagination cursor', () => {
  const filters = { user_id: 'owner&one', cursor: 'list-cursor', limit: 100 };
  expect(conversationHref('thread/one', filters)).toBe(
    '/admin/sessions/thread%2Fone?user_id=owner%26one',
  );
  expect(filters.cursor).toBe('list-cursor');
});
