import type { AgentConfigApi } from '@/lib/api/agents';

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
