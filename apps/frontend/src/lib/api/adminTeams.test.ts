import { afterEach, expect, it, vi } from 'vitest';

import { getAdminTeam, listAdminTeamAudit, listAdminTeams, personLabel } from './adminTeams';
import * as client from './client';

afterEach(() => vi.restoreAllMocks());

it('reads teams uncached, sending only the filters that are set', async () => {
  const fetch = vi
    .spyOn(client, 'fetchWithAuth')
    .mockImplementation(
      async () => new Response('{}', { headers: { 'Content-Type': 'application/json' } }),
    );
  const controller = new AbortController();

  await listAdminTeams({ q: '  ', state: 'active', before: null }, controller.signal);
  await listAdminTeams({ q: ' a&b ', state: 'all', before: 'ateam_9' }, controller.signal);
  await getAdminTeam('ateam/1', controller.signal);
  await listAdminTeamAudit('ateam/1', null, controller.signal);
  await listAdminTeamAudit('ateam/1', 42, controller.signal);

  expect(fetch.mock.calls.map(([, path]) => path)).toEqual([
    '/admin/agent/teams?state=active',
    '/admin/agent/teams?q=a%26b&state=all&before=ateam_9',
    '/admin/agent/teams/ateam%2F1',
    '/admin/agent/teams/ateam%2F1/audit-events',
    '/admin/agent/teams/ateam%2F1/audit-events?before=42',
  ]);
  expect(
    fetch.mock.calls.every(
      ([, , init]) => init?.cache === 'no-store' && init?.signal === controller.signal,
    ),
  ).toBe(true);
});

it('names a person by profile name, then email, then account id', () => {
  const base = { user_id: 'u1', display_name: '', email: null, avatar_data_url: '' };
  expect(personLabel({ ...base, display_name: 'Ada' })).toBe('Ada');
  expect(personLabel({ ...base, email: 'ada@example.com' })).toBe('ada@example.com');
  expect(personLabel(base)).toBe('u1');
});
