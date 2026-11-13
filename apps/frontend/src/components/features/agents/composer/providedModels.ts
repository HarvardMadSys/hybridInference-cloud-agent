import type {
  AgentConfigApi,
  ModelConnectionOptionApi,
  ProvidedModelSourceApi,
} from '@/lib/api/agents';

// Models the deployment provides, as their owner-facing pages read them.
//
// A provided source (`oconn_…`) sits beside the owner's own connections
// (`mconn_…`) and is chosen the same way: a model and the source's id. What
// differs is the allowance. Each owner may send a number of requests per
// model per UTC day, counted by the deployment's relay; `/config` answers the
// allowance, today's use and when the count starts over.

const PROVIDED_PREFIX = 'oconn_';

/** Whether an id names a connection the deployment provides. */
export function isProvidedId(id: string | null | undefined): boolean {
  return typeof id === 'string' && id.startsWith(PROVIDED_PREFIX);
}

/** The provided sources on offer; none where model connections are switched off. */
export function providedSources(config: AgentConfigApi | null): ProvidedModelSourceApi[] {
  return config?.model_connections_enabled === false ? [] : (config?.provided_sources ?? []);
}

/** The owner's own enabled connections on offer. */
export function ownConnections(config: AgentConfigApi | null): ModelConnectionOptionApi[] {
  return config?.model_connections_enabled === false ? [] : (config?.model_connections ?? []);
}

/** The provided source with this id, whether or not it is on offer right now. */
export function providedSource(
  config: AgentConfigApi | null,
  id: string | null | undefined,
): ProvidedModelSourceApi | undefined {
  return id ? config?.provided_sources?.find((source) => source.id === id) : undefined;
}

/** A source by id: a provided one, or one of the owner's own connections. */
export function findSource(
  config: AgentConfigApi | null,
  id: string | null | undefined,
): ModelConnectionOptionApi | undefined {
  if (!id) return undefined;
  return providedSource(config, id) ?? config?.model_connections?.find((entry) => entry.id === id);
}

/** What an owner has left of one provided model today. */
export interface Allowance {
  /** Requests a day; `null` is unlimited. */
  limit: number | null;
  /** Requests accepted today. */
  used: number;
  /** `limit - used`, never below zero; `null` when unlimited. */
  remaining: number | null;
  /** Nothing is left until `resetsAt`. */
  exhausted: boolean;
  resetsAt: string;
}

/**
 * The owner's allowance on one of a source's models, as of `now`.
 *
 * Once `resets_at` has passed the day's count has started over, whatever a
 * `/config` read before midnight said: a page left open overnight must not
 * keep a model closed. A listed model with no allowance entry is not limited
 * here; the server still decides when the turn is sent.
 */
export function allowanceOf(
  source: ProvidedModelSourceApi,
  model: string,
  now: number = Date.now(),
): Allowance {
  const entry = source.allowances?.[model];
  const limit = entry?.daily_requests ?? null;
  const reset = Date.parse(source.resets_at);
  const used = !entry || (Number.isFinite(reset) && now >= reset) ? 0 : entry.used_today;
  const remaining = limit === null ? null : Math.max(0, limit - used);
  return { limit, used, remaining, exhausted: remaining === 0, resetsAt: source.resets_at };
}

/**
 * When the next of the provided sources' counts starts over, in epoch
 * milliseconds; `null` when none lies ahead of `now`.
 */
export function nextAllowanceReset(
  config: AgentConfigApi | null,
  now: number = Date.now(),
): number | null {
  const ahead = providedSources(config)
    .map((source) => Date.parse(source.resets_at))
    .filter((reset) => Number.isFinite(reset) && reset > now);
  return ahead.length > 0 ? Math.min(...ahead) : null;
}

/** A provided model's allowance by its source id; `null` for any other choice. */
export function providedAllowance(
  config: AgentConfigApi | null,
  connectionId: string | null | undefined,
  model: string,
  now?: number,
): Allowance | null {
  const source = providedSource(config, connectionId);
  return source?.models.includes(model) ? allowanceOf(source, model, now) : null;
}

/** Whether any provided source has a model some agent can run for this owner. */
export function hasRunnableProvidedModel(config: AgentConfigApi | null): boolean {
  return providedSources(config).some((source) =>
    source.models.some((model) => (source.model_runtimes?.[model]?.length ?? 0) > 0),
  );
}

/**
 * Whether nothing can run until the owner adds a connection of their own.
 *
 * The server says so (`model_connection_required`), and it already counts a
 * provided model as enough. A provided model that can run is checked here too,
 * so an answer that lists one never sends its owner to set up a key.
 */
export function modelSetupRequired(config: AgentConfigApi | null): boolean {
  return config?.model_connection_required === true && !hasRunnableProvidedModel(config);
}

/** The provided source a newcomer is pointed at: the default one, else the first. */
export function leadingProvidedSource(
  config: AgentConfigApi | null,
): ProvidedModelSourceApi | undefined {
  const sources = providedSources(config);
  return sources.find((source) => source.is_default) ?? sources[0];
}

/** When the day's count starts over, in the viewer's own time zone and format. */
export function resetTime(iso: string): string {
  const time = new Date(iso);
  if (Number.isNaN(time.getTime())) return '';
  return time.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' });
}

/**
 * The line that says whose a turn's usage is: the owner's provider bills a
 * turn on their own connection; one on a provided model spends the owner's
 * daily allowance on this deployment. A control plane that predates provided
 * models names no source, which reads as the owner's own.
 */
export function usageSourceKey(
  usage: { source?: 'personal' | 'provided' | null } | null | undefined,
): 'modelSource.usageProvided' | 'modelSource.usageExternal' {
  return usage?.source === 'provided' ? 'modelSource.usageProvided' : 'modelSource.usageExternal';
}
