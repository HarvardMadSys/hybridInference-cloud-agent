import { useCallback, useMemo, useRef, useState } from 'react';
import type { AgentConfigApi } from '@/lib/api/agents';

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
 */
export function useComposerModel(initialPick: string | undefined) {
  const [models, setModels] = useState<string[]>([]);
  const [chosenModel, chooseModel] = useState(initialPick ?? '');
  const [resolvedModel, setResolvedModel] = useState('');
  const trigger = useRef<HTMLButtonElement | null>(null);
  // The config's model list is the create endpoint's own predicate;
  // /v1/models answers for the browsing user and offered models whose first
  // agent call then 404ed.
  const adoptModels = useCallback((config: AgentConfigApi) => {
    setModels(config.models ?? []);
    setResolvedModel(config.default_model ?? '');
  }, []);
  let model = '';
  if (models.includes(chosenModel)) model = chosenModel;
  else if (models.includes(resolvedModel)) model = resolvedModel;
  // Named apart from the repository, so Run's title says which control to
  // look at, and a Run without one points at the empty control.
  const missingModel = !model && models.length > 0;
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
  return { models, model, chosenModel, chooseModel, adoptModels, missingModel, modelControl };
}
