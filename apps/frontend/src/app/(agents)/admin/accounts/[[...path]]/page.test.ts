import { describe, expect, it } from 'vitest';

import AccountsAddressPage from './page';
import TeamsAddressPage from '../../teams/[[...path]]/page';

async function destination(page: Promise<void>): Promise<string | undefined> {
  try {
    await page;
  } catch (error) {
    return (error as { digest?: string }).digest;
  }
  return undefined;
}

describe('the old Accounts and Teams addresses', () => {
  it.each([
    [[], '/admin/users'],
    [['policy'], '/admin/users/policy'],
    [['mail'], '/admin/users/mail'],
  ])('opens %j under Users', async (path, target) => {
    const digest = await destination(AccountsAddressPage({ params: Promise.resolve({ path }) }));
    expect(digest).toContain(`;${target};`);
  });

  it.each([
    [undefined, '/admin/users/teams'],
    [['ateam_1'], '/admin/users/teams/ateam_1'],
    [['a/b'], '/admin/users/teams/a%2Fb'],
  ])('opens team %j under Users', async (path, target) => {
    const digest = await destination(TeamsAddressPage({ params: Promise.resolve({ path }) }));
    expect(digest).toContain(`;${target};`);
  });
});
