// The real production Next app, with a same-origin HTTP API fixture. No test
// route or auth bypass is compiled into the app. Browser offline mode therefore
// fails actual fetches; page.route().fulfill() cannot bypass the disconnection.
import { createServer, request as proxyRequest } from 'node:http';
import {
  buildStandalone,
  startStandalone,
  verifyStandaloneVariant,
  waitForResponse,
} from '../scripts/standalone-harness.mjs';
import { createFixture } from './service.mjs';

const port = Number(process.env.CLOUD_AGENT_E2E_PORT);
if (!port) throw new Error('Missing isolated browser-test port');
const env = {
  ...process.env,
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
try {
  await waitForResponse(`http://127.0.0.1:${app.port}/login`, {
    server: app,
    timeoutMs: 10_000,
    acceptResponse: (response) => response.ok,
  });
} catch (error) {
  await app.stop();
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
server.listen(port, '127.0.0.1');
let stopping = false;
async function stop() {
  if (stopping) return;
  stopping = true;
  fixture.close();
  server.closeAllConnections();
  await new Promise((resolve) => server.close(resolve));
  await app.stop();
}
app.child.once('exit', (code) => {
  if (stopping) return;
  void stop().finally(() => {
    process.exitCode = code ?? 1;
  });
});
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.once(signal, () => {
    void stop().finally(() => process.exit(0));
  });
}
