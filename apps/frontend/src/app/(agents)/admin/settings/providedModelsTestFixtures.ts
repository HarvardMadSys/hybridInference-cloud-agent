import type {
  OperatorModelConnection,
  OperatorModelConnectionList,
  OperatorModelLimit,
} from '@/lib/api/adminOperatorModels';
import type { ModelStatus } from '@/lib/api/modelConnections';
import { APIError } from '@/lib/utils/errors';

export const PROVIDED_ID = 'oconn_0123456789abcdef0123456789abcdef';

/** A model verified for every agent. */
export function readyModel(model: string): ModelStatus {
  return {
    model,
    status: 'ready',
    category: null,
    message: '',
    runtimes: ['claude-code', 'codex'],
    paths: [],
    protocols: {},
    checked_at: '2026-11-12T00:00:00Z',
  };
}

/** A model the provider refused: no agent can use it. */
export function refusedModel(model: string, category = 'auth'): ModelStatus {
  return { ...readyModel(model), status: 'failed', category, runtimes: [] };
}

/** A daily allowance with no deployment-wide cap. */
export function daily(user_daily_requests: number | null): OperatorModelLimit {
  return { user_daily_requests, global_concurrency: null };
}

/** A saved, disabled connection listing two models, neither with a default allowance. */
export function providedConnection(
  overrides: Partial<OperatorModelConnection> = {},
): OperatorModelConnection {
  const models = overrides.models ?? ['house-fast', 'house-deep'];
  return {
    id: PROVIDED_ID,
    name: 'House models',
    base_url: 'https://api.example.com/v1',
    models,
    default_model: null,
    is_default: false,
    enabled: false,
    end_user_header: null,
    user_concurrency: 4,
    limits: {},
    model_status: models.map(readyModel),
    key_refused_at: null,
    created_at: '2026-11-12T00:00:00Z',
    updated_at: '2026-11-12T00:00:00Z',
    ...overrides,
  };
}

export function providedList(
  connections: OperatorModelConnection[] = [providedConnection()],
  overrides: Partial<OperatorModelConnectionList> = {},
): OperatorModelConnectionList {
  return { connections, relay_hosts: 1, feature_enabled: true, ...overrides };
}

/** A refusal as the API client raises it. */
export function refusal(
  code: string,
  message: string,
  status: number,
  details?: Record<string, unknown>,
): APIError {
  return new APIError(code, message, status, details);
}
