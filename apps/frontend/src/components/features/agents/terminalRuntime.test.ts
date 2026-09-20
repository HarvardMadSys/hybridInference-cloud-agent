// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import { loadTerminalRuntime } from './terminalRuntime';

describe('loadTerminalRuntime', () => {
  it('hands every caller the same load', async () => {
    // The panel starts this when it becomes visible so the fetch runs beside
    // the create request; the pane that mounts afterwards has to join that
    // load rather than start a second one. Sharing the promise is what makes
    // that a property of this module instead of an assumption about webpack.
    const first = loadTerminalRuntime();
    const second = loadTerminalRuntime();

    expect(first).toBe(second);
    await expect(first).resolves.toHaveLength(2);
  });
});
