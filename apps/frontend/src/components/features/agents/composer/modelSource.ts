import type { AgentConfigApi } from '@/lib/api/agents';
import type { Translator } from '@/lib/i18n/translate';
import {
  findSource,
  isProvidedId,
  ownConnections,
  providedAllowance,
  providedSources,
  resetTime,
} from './providedModels';

// Where a model runs is part of the choice, not a detail of it.
//
// Each of the owner's connections is a separate source, and so is each
// connection the deployment provides. The same model id may be offered by
// several of them: `gpt-5` on two keys bills two accounts, and on a provided
// source it spends the owner's daily allowance instead. So a choice is always
// the pair, and two choices are equal only when both halves are. Drafts,
// requests and API state keep the two fields as they are; the encoded key
// below exists only so a picker, which deals in strings, can tell two
// same-named options apart.

/** A model and the connection it runs on; null names no connection, so it cannot run. */
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
 * A choice on a connection is the JSON pair, which starts `["mconn_`. One with
 * no connection keeps its name, or `[null, name]` if that starts with `[`, so
 * the two can never collide.
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
 * Every model a runtime's picker lists: the provided sources' models first,
 * then each of the owner's enabled connections', where this runtime can run
 * them. A model is listed only once its own verification found a way for this
 * agent to use it. A provided model whose allowance is used up for today is
 * listed too, so its owner can see when it comes back; `offeredChoices` leaves
 * it out.
 */
export function listedChoices(config: AgentConfigApi | null, runtime: string): ModelChoice[] {
  return [...providedSources(config), ...ownConnections(config)].flatMap((source) =>
    source.models
      .filter((model) => source.model_runtimes?.[model]?.includes(runtime))
      .map((model) => ({ model, connectionId: source.id })),
  );
}

/** Whether a choice is a provided model with nothing left of today's allowance. */
export function isUsedUp(config: AgentConfigApi | null, choice: ModelChoice, now?: number) {
  return providedAllowance(config, choice.connectionId, choice.model, now)?.exhausted === true;
}

/**
 * What a runtime can run now: the listed models, less any provided one used
 * up for today. Exactly what the server accepts, so nothing is offered and
 * then refused on Run.
 */
export function offeredChoices(config: AgentConfigApi | null, runtime: string): ModelChoice[] {
  return listedChoices(config, runtime).filter((choice) => !isUsedUp(config, choice));
}

/**
 * Whether a choice may not run here: a loaded catalog that is being read again,
 * or one that does not offer its exact source, or offers it with nothing left
 * of today's allowance.
 *
 * Before any catalog has loaded nothing is known, and a turn inheriting its
 * source is left to the server, which checks that connection again on every
 * message and refuses one that cannot serve it. A choice the owner made was
 * picked from a loaded catalog, so it is always checked against one.
 */
export function isModelUnavailable(
  config: AgentConfigApi | null,
  runtime: string,
  choice: ModelChoice,
  refreshing = false,
): boolean {
  if (config === null) return false;
  if (refreshing) return true;
  return !offeredChoices(config, runtime).some((option) => sameChoice(option, choice));
}

/** What the server resolved for a new task, as a choice. */
export function resolvedChoice(config: AgentConfigApi): ModelChoice {
  return storedChoice(config.default_model, config.default_model_connection_id);
}

export interface LabelOptions {
  /** Not on offer here: marked, never swapped for another source. */
  unavailable?: boolean;
  /** The agent it would run with. Kept for callers; which API it uses is never shown. */
  runtime?: string;
}

/** The sources that list a model: the owner's enabled connections and the provided ones. */
function sourcesOf(config: AgentConfigApi | null, model: string): number {
  return [...(config?.provided_sources ?? []), ...(config?.model_connections ?? [])].filter(
    (entry) => entry.models.includes(model),
  ).length;
}

/** A choice's model, with its source's name where another source lists the same model. */
function sourceLabel(config: AgentConfigApi | null, choice: ModelChoice, t: Translator): string {
  const source = findSource(config, choice.connectionId);
  if (!source) {
    const provided = isProvidedId(choice.connectionId);
    return t(provided ? 'modelSource.providedUnknown' : 'modelSource.personalUnknown', {
      model: choice.model,
    });
  }
  return sourcesOf(config, choice.model) > 1
    ? t('modelSource.personal', { model: choice.model, name: source.name })
    : choice.model;
}

/**
 * A choice's model, and its source's name where another source lists the same
 * model, so two sources of one name never read alike; one that cannot run
 * here says so, and a provided one used up for today says that instead. Which
 * API the agent reaches it on is the platform's business and is not part of
 * the label.
 */
export function choiceLabel(
  config: AgentConfigApi | null,
  choice: ModelChoice,
  t: Translator,
  { unavailable = false }: LabelOptions = {},
): string {
  if (!choice.connectionId) return choice.model;
  const label = sourceLabel(config, choice, t);
  if (isUsedUp(config, choice)) return t('modelSource.usedUp', { label });
  return unavailable ? t('modelSource.unavailable', { label }) : label;
}

export interface ModelPickerProps {
  value: string;
  options: string[];
  optionLabels: Record<string, string>;
  onChange: (key: string) => void;
  /**
   * Each option's group heading, set only where the list holds both provided
   * models and the owner's own connections.
   */
  optionGroups?: Record<string, string>;
  /** A provided option's second line: what is left today, or when it resets. */
  optionNotes?: Record<string, string>;
  /** Options shown but not selectable: provided models used up for today. */
  disabledOptions?: string[];
}

/**
 * What a provided model adds to its option: how many requests are left today,
 * or, with none left, when the count starts over (the option is then disabled).
 * Nothing extra for an unlimited allowance or any other source.
 */
function allowanceNote(
  config: AgentConfigApi | null,
  choice: ModelChoice,
  t: Translator,
): { note: string; disabled: boolean } | null {
  const allowance = providedAllowance(config, choice.connectionId, choice.model);
  if (!allowance || allowance.remaining === null) return null;
  if (allowance.exhausted) {
    return {
      note: t('modelSource.resets', { time: resetTime(allowance.resetsAt) }),
      disabled: true,
    };
  }
  return { note: t('modelSource.remaining', { count: allowance.remaining }), disabled: false };
}

/** The group headings, allowance notes and disabled options of a list of choices. */
function pickerAnnotations(
  config: AgentConfigApi | null,
  listed: ModelChoice[],
  keyOf: (choice: ModelChoice) => string,
  t: Translator,
): Pick<ModelPickerProps, 'optionGroups' | 'optionNotes' | 'disabledOptions'> {
  const provided = listed.filter((choice) => isProvidedId(choice.connectionId));
  const grouped = provided.length > 0 && provided.length < listed.length;
  const optionGroups: Record<string, string> = {};
  const optionNotes: Record<string, string> = {};
  const disabledOptions: string[] = [];
  for (const choice of listed) {
    const key = keyOf(choice);
    if (grouped) {
      optionGroups[key] = t(
        isProvidedId(choice.connectionId) ? 'modelSource.providedGroup' : 'modelSource.ownGroup',
      );
    }
    const extra = allowanceNote(config, choice, t);
    if (extra) optionNotes[key] = extra.note;
    if (extra?.disabled) disabledOptions.push(key);
  }
  return {
    ...(grouped ? { optionGroups } : {}),
    ...(Object.keys(optionNotes).length ? { optionNotes } : {}),
    ...(disabledOptions.length ? { disabledOptions } : {}),
  };
}

/**
 * The props a `Picker` needs for these choices (`listedChoices`). `kept` is
 * shown first when it is not listed — the turn a follow-up inherits, or a
 * personal pick that can no longer run — so the control never shows one thing
 * while another runs. Provided models and the owner's own are grouped apart
 * when both are listed, and a provided model shows what is left of today's
 * allowance; one with nothing left stays in the list, disabled.
 */
export function modelPicker(
  config: AgentConfigApi | null,
  listed: ModelChoice[],
  current: ModelChoice,
  onChange: (choice: ModelChoice) => void,
  t: Translator,
  { kept = current, runtime }: { kept?: ModelChoice; runtime?: string } = {},
): ModelPickerProps {
  const missing = kept.model && !listed.some((choice) => sameChoice(choice, kept));
  const choices = missing ? [kept, ...listed] : listed;
  // A model offered once keys by its own name; one offered by several sources
  // keys by the pair, so the picker can tell them apart.
  const named = new Map<string, number>();
  for (const choice of choices) named.set(choice.model, (named.get(choice.model) ?? 0) + 1);
  const keyOf = (choice: ModelChoice) =>
    named.get(choice.model) === 1 && !choice.model.startsWith('[')
      ? choice.model
      : choiceKey(choice);
  const optionLabels: Record<string, string> = {};
  const byKey = new Map<string, ModelChoice>();
  for (const choice of choices) {
    const key = keyOf(choice);
    byKey.set(key, choice);
    optionLabels[key] = choiceLabel(config, choice, t, {
      unavailable: missing === true && choice === kept,
      runtime,
    });
  }
  const shown = choices.find((choice) => sameChoice(choice, current));
  return {
    value: shown ? keyOf(shown) : choiceKey(current),
    options: [...byKey.keys()],
    optionLabels,
    onChange: (key) => {
      const next = byKey.get(key);
      if (next && !isUsedUp(config, next)) onChange(next);
    },
    ...pickerAnnotations(config, listed, keyOf, t),
  };
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
  const listed = listedChoices(config, runtime);
  if (!listed.length) return null;
  return modelPicker(config, listed, current, onChange, t, {
    kept: current.model ? current : inherited,
    runtime,
  });
}

/**
 * What a follow-up must send to run on `choice` instead of `parent`'s source.
 *
 * Nothing when both match: an omitted model inherits the model and its source
 * together. Otherwise the model always goes, with its connection when it has
 * one — a switch between sources of the same model name is still a switch.
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
