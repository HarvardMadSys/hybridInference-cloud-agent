import type { AgentConfigApi, ProvidedModelSourceApi } from '@/lib/api/agents';

/** The connection a test's owner runs on. */
export const TEST_CONNECTION = 'mconn_test';

/** The configuration fields of an owner with one connection listing `models`. */
export function offering(
  models: string[],
  runtimes: string[] = ['claude-code', 'codex', 'pi', 'opencode', 'kilo'],
): Pick<AgentConfigApi, 'model_connections' | 'model_connections_enabled'> {
  return {
    model_connections_enabled: true,
    model_connections: [
      {
        id: TEST_CONNECTION,
        name: 'Mine',
        models,
        model_runtimes: Object.fromEntries(models.map((model) => [model, runtimes])),
      },
    ],
  };
}

/** A connection the deployment provides, as a test's owner is offered it. */
export const TEST_PROVIDED = 'oconn_0123456789abcdef0123456789abcdef';

/** A time safely after any test's clock: today's counts have not started over. */
export const TEST_RESETS_AT = '2999-01-01T00:00:00Z';

/**
 * One provided source listing `allowances`' models: each value is
 * `[daily_requests, used_today]`, with `null` for an unlimited allowance.
 */
export function providedSource(
  allowances: Record<string, [number | null, number]>,
  overrides: Partial<ProvidedModelSourceApi> = {},
  runtimes: string[] = ['claude-code', 'codex', 'pi', 'opencode', 'kilo'],
): ProvidedModelSourceApi {
  const models = Object.keys(allowances);
  return {
    id: TEST_PROVIDED,
    name: 'House models',
    is_default: true,
    default_model: models[0] ?? null,
    models,
    model_runtimes: Object.fromEntries(models.map((model) => [model, runtimes])),
    allowances: Object.fromEntries(
      Object.entries(allowances).map(([model, [daily_requests, used_today]]) => [
        model,
        { daily_requests, used_today },
      ]),
    ),
    resets_at: TEST_RESETS_AT,
    ...overrides,
  };
}
