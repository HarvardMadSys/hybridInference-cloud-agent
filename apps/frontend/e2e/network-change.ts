// Chromium fails every request still waiting for a connection with
// net::ERR_NETWORK_CHANGED when the host's addresses change, as they do on a
// shared CI runner when other jobs start or stop containers. That abort says
// nothing about the app. A spec that uses this `test` and allows one retry
// reruns an attempt that saw such an abort; the retry of any other failure
// fails at once, so that failure still fails the run.
import { access, mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { expect, test as base, type BrowserContext } from '@playwright/test';

export const test = base.extend<{ networkChange: void }>({
  networkChange: [
    async ({ browser, context }, use, testInfo) => {
      // Each attempt runs in a new worker, so the evidence is handed on in a file.
      const evidence = path.join(testInfo.project.outputDir, '.network-changed', testInfo.testId);
      if (testInfo.retry > 0) {
        await access(evidence).catch(() => {
          throw new Error('Not retried: no network change aborted the failed attempt.');
        });
      }
      let aborted = false;
      const watch = (watched: BrowserContext) => {
        watched.on('requestfailed', (request) => {
          if (request.failure()?.errorText === 'net::ERR_NETWORK_CHANGED') aborted = true;
        });
      };
      watch(context);
      browser.on('context', watch);
      try {
        await use();
      } finally {
        browser.off('context', watch);
      }
      if (aborted && testInfo.status !== testInfo.expectedStatus) {
        await mkdir(path.dirname(evidence), { recursive: true });
        await writeFile(evidence, '');
      }
    },
    { auto: true },
  ],
});

export { expect };
