import { config } from '@/config/env';
import { fetchWithAuth, jsonOrThrow } from './client';

const MODEL_PREFERENCE = '/v1/agent/model-preference';

/**
 * The model a new task starts on, and where that choice came from.
 *
 * `default_model` is the saved pin, reported exactly as stored — including a
 * pin the deployment has since retired, which the settings card needs in order
 * to explain why it is not being applied. `last_model` is the model the newest
 * turn ran on. `effective_model` is the one value a new task would actually
 * start on, narrowed to the models the gateway still offers.
 */
export interface ModelPreference {
  default_model: string | null;
  last_model: string | null;
  /**
   * The owner's connection `last_model` ran on; null is the Gateway. While it
   * is set and nothing is pinned, `effective_model` is null: the pin and the
   * deployment default are Gateway models, and a new task offers the
   * connection's model again only while that connection still lists it.
   */
  last_model_connection_id?: string | null;
  effective_model: string | null;
  /**
   * The deployment's starting point, shown to a user with no history of their
   * own. Null when the administrator set none, or when it is not a model this
   * user can call.
   */
  deployment_default: string | null;
  /**
   * The models this owner may pin, as the gateway offers them. Empty means the
   * catalog could not be read — not that every model was retired.
   */
  models: string[];
}

export async function getModelPreference(signal?: AbortSignal): Promise<ModelPreference> {
  return jsonOrThrow<ModelPreference>(
    await fetchWithAuth(config.apiBase, MODEL_PREFERENCE, { signal, cache: 'no-store' }),
  );
}

/**
 * Pin a model, or pass `null` to go back to following the last used one.
 *
 * The empty string is refused here rather than sent: the control that clears
 * the pin has no model, and an empty name is not a model.
 */
export async function saveModelPreference(
  defaultModel: string | null,
  signal?: AbortSignal,
): Promise<ModelPreference> {
  if (defaultModel === '') throw new Error('Choose a model, or follow the last used one.');
  return jsonOrThrow<ModelPreference>(
    await fetchWithAuth(config.apiBase, MODEL_PREFERENCE, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ default_model: defaultModel }),
      signal,
    }),
  );
}
