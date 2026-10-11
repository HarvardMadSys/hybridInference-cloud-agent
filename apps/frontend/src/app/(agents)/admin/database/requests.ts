import { APIError } from '@/lib/utils/errors';

export function accessDenied(cause: unknown): boolean {
  return cause instanceof APIError && (cause.statusCode === 401 || cause.statusCode === 403);
}

export function trackedRequest(trackRequest: DatabaseAccessProps['trackRequest']) {
  const controller = new AbortController();
  return { controller, release: trackRequest(controller) };
}

export interface DatabaseAccessProps {
  trackRequest: (controller: AbortController) => () => void;
  onDenied: () => void;
}
