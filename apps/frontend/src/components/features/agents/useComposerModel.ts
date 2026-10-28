import { useCallback, useMemo, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import type { AgentConfigApi } from '@/lib/api/agents';
import {
  modelPicker,
  NO_MODEL,
  offeredChoices,
  resolvedChoice,
  rewriteProps,
  sameChoice,
  storedChoice,
  type ModelChoice,
} from './modelSource';

/** The pick a draft kept, with its source; see `NewTaskDraft`. */
interface DraftedPick {
  chosenModel: string;
  chosenModelConnectionId: string;
}

/**
 * The model a new task starts on, as two values, never one: what the owner
 * picked in the composer, which a draft keeps, and what the server resolved
 * for them, which is read again on every visit so a pin saved in Settings
 * meanwhile takes effect. The control shows the pick while it is on offer,
 * else the resolved model. A fallback is therefore never mistaken for a pick
 * and never saved as one.
 *
 * `default_model` is what the server resolved for *this owner*: their pin,
 * else the model their last task used, else the deployment default. It is
 * the only thing allowed to fill the control in. There is deliberately no
 * "first model in the list" fallback. The model decides what runs the task —
 * models differ in cost, speed and what they can do — so picking one on the
 * owner's behalf is the same mistake as picking a repository for them, which
 * the composer already refuses to do. With nothing resolved the control
 * stays empty and Run waits, exactly as it does for an unset repository.
 *
 * Both values carry their source (see `modelSource`). A model on the owner's
 * own connection that cannot run here — another agent was chosen, or the
 * connection was disabled or no longer lists it — stays on screen marked
 * unavailable and holds Run. It never gives way to the resolved model, or to
 * the Gateway model of the same name: either would bill someone else.
 */
export function useComposerModel(drafted: DraftedPick | null, runtime: string) {
  const t = useT();
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  const [chosen, choose] = useState<ModelChoice>(() =>
    drafted ? storedChoice(drafted.chosenModel, drafted.chosenModelConnectionId) : NO_MODEL,
  );
  const [resolved, setResolved] = useState<ModelChoice>(NO_MODEL);
  const trigger = useRef<HTMLButtonElement | null>(null);
  // The config's model list is the create endpoint's own predicate;
  // /v1/models answers for the browsing user and offered models whose first
  // agent call then 404ed.
  const adoptModels = useCallback((loaded: AgentConfigApi) => {
    setConfig(loaded);
    setResolved(resolvedChoice(loaded));
  }, []);
  const offered = useMemo(() => offeredChoices(config, runtime), [config, runtime]);
  let choice = NO_MODEL;
  let unavailable: ModelChoice | null = null;
  for (const candidate of [chosen, resolved]) {
    if (!candidate.model) continue;
    if (offered.some((option) => sameChoice(option, candidate))) {
      choice = candidate;
      break;
    }
    // A Gateway model no longer offered gives way, as it always has.
    if (candidate.connectionId) {
      unavailable = candidate;
      break;
    }
  }
  // Named apart from the repository, so Run's title says which control to
  // look at, and a Run without one points at the empty control.
  const missingModel = !choice.model && (offered.length > 0 || unavailable !== null);
  const picker = modelPicker(config, offered, unavailable ?? choice, choose, t, { runtime });
  const modelControl = useMemo(
    () => ({
      attach: (element: HTMLButtonElement | null) => {
        trigger.current = element;
      },
      pointIfMissing: () => {
        if (missingModel) trigger.current?.focus();
      },
    }),
    [missingModel],
  );
  // What a draft keeps: the pick and its source, never the model shown.
  const draftFields = useMemo(
    () => ({ chosenModel: chosen.model, chosenModelConnectionId: chosen.connectionId ?? '' }),
    [chosen],
  );
  return {
    choice,
    draftFields,
    // What a create request names: the model, and its connection when it has one.
    requestFields: {
      model: choice.model,
      ...(choice.connectionId ? { model_connection_id: choice.connectionId } : {}),
    },
    rewrite: rewriteProps(choice, t),
    // Said beside the composer while a personal pick holds Run.
    unavailableHint: unavailable
      ? t('modelSource.unavailableHint', { label: picker.optionLabels[picker.value], runtime })
      : '',
    adoptModels,
    missingModel,
    picker,
    modelControl,
  };
}
