import 'server-only';
import { connection } from 'next/server';
import type { AuthConfig } from '@/lib/api/accounts';

/** Read the public sign-in settings per request so the first document shows them. */
export async function readRuntimeAuthConfig(): Promise<AuthConfig | null> {
  await connection();
  const api = (process.env['AGENT_CONTROL_PLANE_URL'] || '').replace(/\/+$/, '');
  // Without a server address the browser reads them itself.
  if (!api) return null;
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
    if (!response.ok) return null;
    const value: unknown = await response.json();
    if (!value || typeof value !== 'object') return null;
    const config = value as AuthConfig;
    if (typeof config.signup_enabled !== 'boolean') return null;
    return { signup_enabled: config.signup_enabled };
  })();
  try {
    return await Promise.race([read, deadline]);
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
