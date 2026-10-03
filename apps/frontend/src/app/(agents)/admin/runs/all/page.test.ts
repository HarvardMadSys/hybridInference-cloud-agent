import { describe, expect, it } from 'vitest';

import AllTurnsPage from './page';

describe('the old all-runs address', () => {
  it('opens the Turns level of the Activity tab', () => {
    let thrown: unknown;
    try {
      AllTurnsPage();
    } catch (error) {
      thrown = error;
    }
    expect((thrown as { digest?: string } | undefined)?.digest).toContain(
      ';/admin/activity?level=turns;',
    );
  });
});
