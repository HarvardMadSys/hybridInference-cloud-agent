import { onTestFinished, vi } from 'vitest';

/**
 * Report `platform` as the browser's `navigator.platform` for one test:
 * "MacIntel" on a Mac, "Win32" on Windows. jsdom reports none, which reads as
 * not Apple. The real value comes back when the test finishes.
 */
export function onPlatform(platform: string): void {
  const spy = vi.spyOn(navigator, 'platform', 'get').mockReturnValue(platform);
  onTestFinished(() => spy.mockRestore());
}
