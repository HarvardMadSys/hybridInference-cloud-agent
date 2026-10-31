export const ERROR_MESSAGES: Record<string, string> = {
  // Authentication errors. Only codes this service's responses can still
  // carry: the structured branch passes the control plane's `type` through,
  // the legacy branch passes `error_code` through, and jsonOrThrow infers
  // nothing from message wording.
  ACCOUNT_SUSPENDED: 'Account has been suspended. Please contact support',
  SESSION_NOT_FOUND: 'Session not found. Please login again',
  SESSION_REVOKED: 'Session has been revoked',

  // API Key errors
  DUPLICATE_API_KEY: 'You already have an active API key',
  API_KEY_NOT_FOUND: 'API key not found',

  // Quota errors
  QUOTA_EXCEEDED: 'Daily usage quota exceeded',

  // Rate limiting
  RATE_LIMIT_EXCEEDED: 'Too many requests. Please try again later',

  // Network errors
  NETWORK_ERROR: 'Network error. Please check your connection',
  TIMEOUT_ERROR: 'Request timeout. Please try again',
  SERVICE_UNAVAILABLE: 'Service temporarily unavailable. Please try again shortly',
  SERVER_ERROR: 'Server error. Please try again in a moment',

  // Default
  UNKNOWN_ERROR: 'An unknown error occurred. Please try again',
};

// Map an HTTP status to a curated error code for responses whose body is not
// JSON (typically an edge proxy / load balancer HTML error page rather than a
// structured backend error). NETWORK_ERROR is deliberately NOT used here: a
// response arrived, so the connection is fine — that code is reserved for true
// fetch() rejections (see safeFetch in lib/api/client.ts).
const TIMEOUT_STATUSES = new Set([408, 504, 522, 524, 598]);
const UNAVAILABLE_STATUSES = new Set([502, 503, 521, 523]);

export function httpStatusToErrorCode(status: number): string {
  if (status === 429) return 'RATE_LIMIT_EXCEEDED';
  if (TIMEOUT_STATUSES.has(status)) return 'TIMEOUT_ERROR';
  if (UNAVAILABLE_STATUSES.has(status)) return 'SERVICE_UNAVAILABLE';
  if (status >= 500) return 'SERVER_ERROR';
  // Non-JSON 4xx (e.g. a 413/404 HTML page): keep the raw HTTP context.
  return 'UNKNOWN_ERROR';
}

export function getErrorMessage(error: unknown): string {
  // Codes come from response bodies, so the table is read by own entry only:
  // `__proto__` must not find Object.prototype and return it as the message.
  if (error instanceof APIError) {
    if (error.code && error.code !== 'UNKNOWN_ERROR' && Object.hasOwn(ERROR_MESSAGES, error.code)) {
      return ERROR_MESSAGES[error.code];
    }
    return error.message || ERROR_MESSAGES.UNKNOWN_ERROR;
  }

  if (error instanceof Error) {
    const errorWithCode = error as Error & { code?: string };
    if (
      errorWithCode.code &&
      errorWithCode.code !== 'UNKNOWN_ERROR' &&
      Object.hasOwn(ERROR_MESSAGES, errorWithCode.code)
    ) {
      return ERROR_MESSAGES[errorWithCode.code];
    }
    return error.message || ERROR_MESSAGES.UNKNOWN_ERROR;
  }

  return ERROR_MESSAGES.UNKNOWN_ERROR;
}

export class APIError extends Error {
  constructor(
    public code: string,
    message: string,
    public statusCode?: number,
    public details?: Record<string, unknown>,
  ) {
    super(message);
    this.name = 'APIError';
  }
}
