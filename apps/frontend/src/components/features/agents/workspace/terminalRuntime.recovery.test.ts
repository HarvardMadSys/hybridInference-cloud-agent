// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

afterEach(() => {
  vi.doUnmock('@xterm/xterm');
  vi.doUnmock('@xterm/addon-fit');
  vi.resetModules();
});

describe('terminal bundle recovery', () => {
  it('can open a terminal after the first bundle download failed', async () => {
    vi.resetModules();
    vi.doMock('@xterm/xterm', () => {
      throw new Error('Loading terminal chunk failed: temporary network error');
    });
    vi.doMock('@xterm/addon-fit', () => ({ FitAddon: class FitAddon {} }));
    const { loadTerminalRuntime } = await import('./terminalRuntime');
    const first = loadTerminalRuntime();
    expect(loadTerminalRuntime()).toBe(first);
    // Vitest wraps failed import factories; the rejection is the browser's
    // failed chunk load at this boundary, regardless of its error wording.
    await expect(first).rejects.toThrow();

    // The bundle server recovered. A new pane may now import the module.
    vi.doMock('@xterm/xterm', () => ({ Terminal: class Terminal {} }));
    const retry = loadTerminalRuntime();
    await expect(retry).resolves.toHaveLength(2);
    expect(loadTerminalRuntime()).toBe(retry);
  });
});
