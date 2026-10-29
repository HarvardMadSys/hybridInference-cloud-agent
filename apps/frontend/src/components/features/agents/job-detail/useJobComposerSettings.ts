'use client';

import { useT } from '@/components/providers';
import { getAgentConfig, type AgentConfigApi, type AgentJobApi } from '@/lib/api/agents';
import { useEffect, useMemo, useState } from 'react';
import type { InheritedModel } from '../ConversationComposer';
import { highestEffort, offeredEfforts } from '../effort';
import {
  choiceLabel,
  offeredChoices,
  rewriteProps,
  sameChoice,
  turnChoice,
  turnModelPicker,
  type ModelChoice,
} from '../modelSource';
import type { AgentJob } from '../types';

/**
 * What a follow-up inherits when it names nothing: the thread's newest turn as
 * the API last listed it, when that is newer than the turn on screen, else the
 * turn on screen. Both carry the model's source.
 */
export function inheritedFrom(parentTurn: AgentJobApi | undefined, job: AgentJob): InheritedModel {
  if (!parentTurn) {
    return {
      model: job.model,
      modelConnectionId: job.modelConnectionId ?? null,
      reasoningEffort: job.reasoningEffort,
    };
  }
  return {
    model: parentTurn.model,
    modelConnectionId: parentTurn.model_connection_id ?? null,
    reasoningEffort: parentTurn.reasoning_effort ?? undefined,
  };
}

/**
 * Load deployment options, and the model and effort the next message runs on.
 *
 * `inherited` holds the model, its source and the effort of the turn a
 * follow-up inherits from: the thread's latest, which need not be the one on
 * screen. A model or effort the owner has not picked is that turn's.
 */
export function useJobComposerSettings(job: AgentJob, inherited: InheritedModel) {
  const t = useT();
  const threadId = job.threadId ?? job.id;
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  // The model and effort the owner picked; `null` inherits the latest turn's.
  const [chosenModel, setChosenModel] = useState<ModelChoice | null>(null);
  const [chosenEffort, setChosenEffort] = useState<string | null>(null);
  // Unsent composer choices belong to the conversation, including when a PR
  // action advances it to another turn without sending the owner's draft.
  useEffect(() => {
    setChosenModel(null);
    setChosenEffort(null);
  }, [threadId]);
  const { model: inheritedModel, modelConnectionId: inheritedSource } = inherited;
  const inheritedChoice = useMemo(
    () => turnChoice({ model: inheritedModel, modelConnectionId: inheritedSource }),
    [inheritedModel, inheritedSource],
  );
  const choice = chosenModel ?? inheritedChoice;
  // The model remains a per-turn choice. A retained live chat reuses its CLI
  // when the process-fixed settings match; changing the model makes only that
  // member process restart and native-resume inside the same sandbox.
  useEffect(() => {
    let cancelled = false;
    getAgentConfig()
      .then((loaded) => {
        if (!cancelled) setConfig(loaded);
      })
      .catch(() => {
        // A list we could not load is not a list to pick from: the composer
        // falls back to naming the inherited model, which is what a turn with
        // no override runs on anyway.
        if (!cancelled) setConfig(null);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const offered = useMemo(() => offeredChoices(config, job.runtime), [config, job.runtime]);
  // The parent's model leads even when this deployment has stopped offering
  // it, so the control cannot show one model while the turn would run another.
  // A personal one that can no longer run is marked so, and is still never
  // swapped for the Gateway model of the same name.
  const modelPickerProps = turnModelPicker(
    config,
    job.runtime,
    choice,
    setChosenModel,
    t,
    inheritedChoice,
  );
  const inheritedUnavailable = Boolean(
    config &&
    inheritedChoice.connectionId &&
    !offered.some((option) => sameChoice(option, inheritedChoice)),
  );

  // Against the model this turn would run — the switched-to one, or the
  // inherited one — because a pair offered last turn need not be offered now.
  const efforts = useMemo(
    () => offeredEfforts(config, job.runtime, choice.model, choice.connectionId),
    [config, job.runtime, choice.model, choice.connectionId],
  );
  // The chosen level leads, then the inherited one; a turn that inherited
  // nothing, or one whose switched-to model does not offer that level, falls to
  // the deepest the new pair has. Only once the config has arrived. Before that
  // the offered set is empty because nothing is *known*, not because nothing is
  // offered — and acting on it would wipe the inherited level a moment before
  // the answer lands.
  const wantedEffort = chosenEffort ?? inherited.reasoningEffort ?? '';
  const effort = !config || efforts.includes(wantedEffort) ? wantedEffort : highestEffort(efforts);

  return {
    config,
    inherited,
    inheritedLabel: choiceLabel(config, inheritedChoice, t, {
      unavailable: inheritedUnavailable,
      runtime: job.runtime,
    }),
    // Holds Send while the turn would inherit a source that can no longer run.
    inheritedUnavailable: inheritedUnavailable && sameChoice(choice, inheritedChoice),
    choice,
    rewrite: rewriteProps(choice, t),
    effort,
    setChosenEffort,
    modelPicker: modelPickerProps,
    efforts,
  };
}
