// The real production Next app, with a same-origin HTTP API fixture. No test
// route or auth bypass is compiled into the app. Browser offline mode therefore
// fails actual fetches; page.route().fulfill() cannot bypass the disconnection.
import { createServer, request as proxyRequest } from 'node:http';
import {
  buildStandalone,
  sanitizeRuntimeSiteEnvironment,
  startStandalone,
  verifyStandaloneVariant,
  waitForResponse,
} from '../scripts/standalone-harness.mjs';
import { createE2EServerLifecycle, unexpectedExitCode } from '../scripts/e2e-server-lifecycle.mjs';
import { createFixture } from './service.mjs';

const port = Number(process.env.CLOUD_AGENT_E2E_PORT);
if (!port) throw new Error('Missing isolated browser-test port');
const env = {
  ...sanitizeRuntimeSiteEnvironment(process.env),
  NEXT_TELEMETRY_DISABLED: '1',
  NEXT_PUBLIC_BASE_PATH: '',
  NEXT_PUBLIC_AGENT_API_URL: '/api',
  AGENT_WEB_APP_NAME: 'Browser Regression Fixture',
  AGENT_WEB_GATEWAY_URL: '',
  AGENT_CONTROL_PLANE_URL: '',
};
const variant = { name: 'web-root', basePath: '', apiUrl: '/api' };
if (process.env.CLOUD_AGENT_E2E_SKIP_BUILD !== '1') {
  buildStandalone(variant, { env });
}
verifyStandaloneVariant(variant);
const app = await startStandalone({ env });
const lifecycle = createE2EServerLifecycle({
  app,
  onUnexpectedAppExit(code, error) {
    if (error) console.error(error);
    process.exitCode = unexpectedExitCode(code);
  },
});
try {
  await waitForResponse(`http://127.0.0.1:${app.port}/login`, {
    server: app,
    timeoutMs: 10_000,
    acceptResponse: (response) => response.ok,
  });
} catch (error) {
  await lifecycle.stop();
  throw error;
}
const fixture = createFixture();
const server = createServer(async (req, res) => {
  try {
    if (await fixture.handle(req, res)) return;
    const upstream = proxyRequest(
      {
        hostname: '127.0.0.1',
        port: app.port,
        path: req.url,
        method: req.method,
        headers: req.headers,
      },
      (reply) => {
        res.writeHead(reply.statusCode, reply.headers);
        reply.pipe(res);
      },
    );
    upstream.on('error', () => {
      if (!res.headersSent) res.writeHead(502);
      res.end('Browser-test app unavailable');
    });
    req.pipe(upstream);
  } catch (error) {
    console.error(error);
    res.writeHead(500).end('Fixture error');
  }
});
await lifecycle.listen({ proxy: server, fixture, port });
