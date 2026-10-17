// The real production Next app, with a same-origin HTTP API fixture. No test
// route or auth bypass is compiled into the app. Browser offline mode therefore
// fails actual fetches; page.route().fulfill() cannot bypass the disconnection.
import { spawn } from 'node:child_process';
import { cpSync, existsSync } from 'node:fs';
import { createServer, request as proxyRequest } from 'node:http';
import { createServer as createPortReservation } from 'node:net';
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
const next = 'node_modules/next/dist/bin/next';
if (process.env.CLOUD_AGENT_E2E_SKIP_BUILD !== '1') {
  const build = spawn(process.execPath, [next, 'build'], { env, stdio: 'inherit' });
  const code = await new Promise((resolve, reject) => {
    build.once('error', reject);
    build.once('exit', resolve);
  });
  if (code !== 0) process.exit(code ?? 1);
}
const reservation = createPortReservation();
await new Promise((resolve) => reservation.listen(0, '127.0.0.1', resolve));
const nextPort = reservation.address().port;
await new Promise((resolve) => reservation.close(resolve));
cpSync('.next/static', '.next/standalone/.next/static', { recursive: true });
if (existsSync('public')) cpSync('public', '.next/standalone/public', { recursive: true });
const app = spawn(process.execPath, ['.next/standalone/server.js'], {
  env: { ...env, PORT: String(nextPort), HOSTNAME: '127.0.0.1' },
  stdio: 'inherit',
});
app.once('error', (error) => {
  console.error(error);
  process.exit(1);
});
app.once('exit', (code) => process.exit(code ?? 1));
let ready = false;
for (let attempt = 0; attempt < 100; attempt += 1) {
  try {
    ready = (await fetch(`http://127.0.0.1:${nextPort}/login`)).ok;
  } catch {
    // Await this owned process, never an unrelated server at a reused port.
  }
  if (ready) break;
  await new Promise((resolve) => setTimeout(resolve, 100));
}
if (!ready) {
  app.kill('SIGTERM');
  throw new Error('The browser-test Next server did not become ready');
}
const fixture = createFixture();
const server = createServer(async (req, res) => {
  try {
    if (await fixture.handle(req, res)) return;
    const upstream = proxyRequest(
      {
        hostname: '127.0.0.1',
        port: nextPort,
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
for (const signal of ['SIGTERM', 'SIGINT']) {
  process.on(signal, () => {
    fixture.close();
    server.closeAllConnections();
    server.close();
    app.kill('SIGTERM');
  });
}
