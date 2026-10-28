// What the control plane answers for owner model connections, for browser
// fixtures. Statuses mirror `application.model_connection_status`.

export const ALL_AGENTS = ['claude-code', 'codex', 'kilo', 'opencode', 'pi'];

export function unverified(model: string) {
  return {
    model,
    status: 'unverified',
    category: null as string | null,
    message: '',
    runtimes: [] as string[],
    paths: [] as Record<string, string>[],
    protocols: {} as Record<string, Record<string, unknown>>,
    checked_at: null as string | null,
  };
}

const supported = { status: 'supported', category: 'supported', message: '', http_status: null };
const missing = {
  status: 'unsupported',
  category: 'endpoint_missing',
  message: 'The provider has no such endpoint at this Base URL.',
  http_status: 404,
};

/** Only Chat Completions works: Codex and Claude Code reach it translated. */
export function chatOnly(model: string) {
  return {
    ...unverified(model),
    status: 'ready',
    runtimes: ALL_AGENTS,
    paths: [
      {
        runtime: 'claude-code',
        mode: 'translated',
        client_protocol: 'anthropic_messages',
        upstream_protocol: 'chat_completions',
      },
      {
        runtime: 'codex',
        mode: 'translated',
        client_protocol: 'responses',
        upstream_protocol: 'chat_completions',
      },
      ...['kilo', 'opencode', 'pi'].map((runtime) => ({
        runtime,
        mode: 'direct',
        client_protocol: 'chat_completions',
        upstream_protocol: 'chat_completions',
      })),
    ],
    protocols: { chat_completions: supported, responses: missing, anthropic_messages: missing },
    checked_at: '2026-10-28T08:00:00Z',
  };
}

/** Only Anthropic Messages works: Claude Code natively, and nothing else. */
export function messagesOnly(model: string) {
  return {
    ...unverified(model),
    status: 'ready',
    runtimes: ['claude-code'],
    paths: [
      {
        runtime: 'claude-code',
        mode: 'direct',
        client_protocol: 'anthropic_messages',
        upstream_protocol: 'anthropic_messages',
      },
    ],
    protocols: { chat_completions: missing, responses: missing, anthropic_messages: supported },
    checked_at: '2026-10-28T08:00:00Z',
  };
}

type Status = ReturnType<typeof unverified>;

/** A model whose verification found no API an agent can use. */
export function refusedStatus(model: string, message: string) {
  return { ...unverified(model), status: 'unsupported', category: 'no_tool_call', message };
}

/** The 422 a save answers when a model could not be verified: nothing was stored. */
export function verificationRefusal(models: Status[]) {
  return {
    detail: {
      error: {
        type: 'model_connection_verification_failed',
        message: 'A model could not be verified for any agent. Nothing was saved.',
        models,
      },
    },
  };
}

/** A saved connection, with each model's status. */
export function connectionView(fields: {
  id: string;
  name: string;
  base_url: string;
  model_status: Status[];
  updated_at?: string;
}) {
  return {
    enabled: true,
    has_api_key: true,
    created_at: '2026-10-28',
    updated_at: '2026-10-28',
    ...fields,
    models: fields.model_status.map((status) => status.model),
    runtimes: [...new Set(fields.model_status.flatMap((status) => status.runtimes))].sort(),
  };
}
