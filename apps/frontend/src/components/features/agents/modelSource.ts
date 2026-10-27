import type { AgentConfigApi } from '@/lib/api/agents';
import type { Translator } from '@/lib/i18n/translate';

// Where a model runs is part of the choice, not a detail of it.
//
// The Gateway and each of the owner's own connections are separate sources,
// and the same model id may be offered by several of them: `gpt-5` on the
// Gateway and `gpt-5` on a personal key bill different accounts. So a choice
// is always the pair, and two choices are equal only when both halves are.
// Drafts, requests and API state keep the two fields as they are; the encoded
// key below exists only so a picker, which deals in strings, can tell two
// same-named options apart. A raw model string saved before sources existed
// is a Gateway model and is never decoded as a key.

/** A model and the source it runs on. `connectionId` null is the Gateway. */
export interface ModelChoice {
  model: string;
  connectionId: string | null;
}

export const NO_MODEL: ModelChoice = { model: '', connectionId: null };

export function sameChoice(a: ModelChoice, b: ModelChoice): boolean {
  return a.model === b.model && (a.connectionId ?? null) === (b.connectionId ?? null);
}

/** The model a turn ran on, with its source. */
export function turnChoice(turn: {
  model: string;
  modelConnectionId?: string | null;
}): ModelChoice {
  return { model: turn.model, connectionId: turn.modelConnectionId ?? null };
}

/**
 * A picker's identity for a choice; `''` for none. Never stored or sent.
 *
 * A Gateway model keeps its own name, as before sources existed; a personal
 * one is the JSON pair, which starts `["mconn_`. A Gateway name that itself
 * starts with `[` is encoded as `[null, name]`, so the two can never collide.
 */
export function choiceKey(choice: ModelChoice): string {
  if (!choice.model) return '';
  if (!choice.connectionId && !choice.model.startsWith('[')) return choice.model;
  return JSON.stringify([choice.connectionId ?? null, choice.model]);
}

/** A choice read from a stored draft, whose fields may be missing or malformed. */
export function storedChoice(model: unknown, connectionId: unknown): ModelChoice {
  if (typeof model !== 'string' || !model) return NO_MODEL;
  return {
    model,
    connectionId: typeof connectionId === 'string' && connectionId ? connectionId : null,
  };
}

/**
 * What a runtime can run: the Gateway's models, then each enabled connection
 * that speaks the runtime's protocol. A connection that cannot serve this
 * runtime is left out rather than offered and refused on Run.
 */
export function offeredChoices(config: AgentConfigApi | null, runtime: string): ModelChoice[] {
  const gateway = (config?.models ?? []).map((model) => ({ model, connectionId: null }));
  const personal = (config?.model_connections ?? [])
    .filter((connection) => connection.runtimes.includes(runtime))
    .flatMap((connection) =>
      connection.models.map((model) => ({ model, connectionId: connection.id })),
    );
  return [...gateway, ...personal];
}

/** What the server resolved for a new task, as a choice. */
export function resolvedChoice(config: AgentConfigApi): ModelChoice {
  return storedChoice(config.default_model, config.default_model_connection_id);
}

/**
 * A personal choice names its connection, so it never reads as the Gateway
 * model of the same name; one that cannot run here says so.
 */
export function choiceLabel(
  config: AgentConfigApi | null,
  choice: ModelChoice,
  t: Translator,
  unavailable = false,
): string {
  if (!choice.connectionId) return choice.model;
  const name = config?.model_connections?.find((entry) => entry.id === choice.connectionId)?.name;
  const label = name
    ? t('modelSource.personal', { model: choice.model, name })
    : t('modelSource.personalUnknown', { model: choice.model });
  return unavailable ? t('modelSource.unavailable', { label }) : label;
}

export interface ModelPickerProps {
  value: string;
  options: string[];
  optionLabels: Record<string, string>;
  onChange: (key: string) => void;
}

/**
 * The props a `Picker` needs for these choices. `kept` is shown first when it
 * is not on offer — the turn a follow-up inherits, or a personal pick that can
 * no longer run — so the control never shows one thing while another runs.
 */
export function modelPicker(
  config: AgentConfigApi | null,
  offered: ModelChoice[],
  current: ModelChoice,
  onChange: (choice: ModelChoice) => void,
  t: Translator,
  kept: ModelChoice = current,
): ModelPickerProps {
  const missing = kept.model && !offered.some((choice) => sameChoice(choice, kept));
  const choices = missing ? [kept, ...offered] : offered;
  const optionLabels: Record<string, string> = {};
  const byKey = new Map<string, ModelChoice>();
  for (const choice of choices) {
    const key = choiceKey(choice);
    byKey.set(key, choice);
    optionLabels[key] = choiceLabel(config, choice, t, missing === true && choice === kept);
  }
  return {
    value: choiceKey(current),
    options: [...byKey.keys()],
    optionLabels,
    onChange: (key) => {
      const next = byKey.get(key);
      if (next) onChange(next);
    },
  };
}

/**
 * The prompt rewriter's model. It runs on the Gateway, so a personal choice
 * turns it off and says why, rather than sending that model to the Gateway.
 */
export function rewriteProps(choice: ModelChoice, t: Translator) {
  return choice.connectionId
    ? { model: '', disabledReason: t('modelSource.rewriteUnavailable') }
    : { model: choice.model };
}

/**
 * The picker for one turn of a conversation on `runtime`, or null when there
 * is nothing to offer — the caller then names the model it inherits.
 */
export function turnModelPicker(
  config: AgentConfigApi | null,
  runtime: string,
  current: ModelChoice,
  onChange: (choice: ModelChoice) => void,
  t: Translator,
  inherited: ModelChoice,
): ModelPickerProps | null {
  const offered = offeredChoices(config, runtime);
  return offered.length ? modelPicker(config, offered, current, onChange, t, inherited) : null;
}

/**
 * What a follow-up must send to run on `choice` instead of `parent`'s source.
 *
 * Nothing when both match: an omitted model inherits the model and its source
 * together. Otherwise the model always goes, with its connection when it has
 * one — a switch between sources of the same model name is still a switch,
 * and a model sent without a connection is the Gateway's.
 */
export function modelOverride(
  parent: { model: string; modelConnectionId?: string | null },
  choice: ModelChoice,
): { model?: string; model_connection_id?: string } {
  if (!choice.model || sameChoice(turnChoice(parent), choice)) return {};
  return {
    model: choice.model,
    ...(choice.connectionId ? { model_connection_id: choice.connectionId } : {}),
  };
}
