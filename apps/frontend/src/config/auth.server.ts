import 'server-only';
import { connection } from 'next/server';
import type { AuthConfig } from '@/lib/api/accounts';

/** Read the public login mode per request so the initial document uses the right entry. */
export async function readRuntimeAuthConfig(): Promise<AuthConfig | null> {
  await connection();
  const api = (process.env['AGENT_CONTROL_PLANE_URL'] || '').replace(/\/+$/, '');
  // The pre-account deployment default, also used by the control plane. Local
  // deployments configure the server address for runtime appearance and mode.
  if (!api) return { mode: 'gateway', signup_enabled: false };
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<null>((resolve) => {
    timer = setTimeout(() => {
      resolve(null);
      controller.abort();
    }, 400);
  });
  const read = (async () => {
    const response = await fetch(`${api}/v1/auth/config`, {
      cache: 'no-store',
      signal: controller.signal,
    });
    if (response.status === 404) return { mode: 'gateway', signup_enabled: false } as const;
    if (!response.ok) return null;
    const value: unknown = await response.json();
    if (!value || typeof value !== 'object') return null;
    const config = value as AuthConfig;
    if (!['gateway', 'local'].includes(config.mode) || typeof config.signup_enabled !== 'boolean')
      return null;
    return { mode: config.mode, signup_enabled: config.signup_enabled };
  })();
  try {
    return await Promise.race([read, deadline]);
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
