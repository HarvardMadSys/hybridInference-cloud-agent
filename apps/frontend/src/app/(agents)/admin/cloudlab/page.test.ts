import { describe, expect, it } from 'vitest';

import AdminCloudLabPage from './page';

describe('the old CloudLab address', () => {
  it('opens the CloudLab nested tab in Settings', () => {
    let thrown: unknown;
    try {
      AdminCloudLabPage();
    } catch (error) {
      thrown = error;
    }
    expect((thrown as { digest?: string } | undefined)?.digest).toContain(
      ';/admin/settings?tab=cloudlab;',
    );
  });
});
