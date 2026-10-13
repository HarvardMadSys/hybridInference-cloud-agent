'use client';

import { getAgentConfig, type AgentConfigApi } from '@/lib/api/agents';
import { useEffect, useMemo, useState } from 'react';
import { composerModels } from '../ConversationComposer';
import { highestEffort, offeredEfforts } from '../effort';
import type { AgentJob } from '../types';

/**
 * Load deployment options, and the model and effort the next message runs on.
 *
 * `inherited` holds the model and effort of the turn a follow-up inherits
 * from: the thread's latest, which need not be the one on screen. A model or
 * effort the owner has not picked is that turn's.
 */
export function useJobComposerSettings(
  job: AgentJob,
  inherited: Pick<AgentJob, 'model' | 'reasoningEffort'>,
) {
  const threadId = job.threadId ?? job.id;
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  const [models, setModels] = useState<string[]>([]);
  // The model and effort the owner picked; `null` inherits the latest turn's.
  const [chosenModel, setChosenModel] = useState<string | null>(null);
  const [chosenEffort, setChosenEffort] = useState<string | null>(null);
  // Unsent composer choices belong to the conversation, including when a PR
  // action advances it to another turn without sending the owner's draft.
  useEffect(() => {
    setChosenModel(null);
    setChosenEffort(null);
  }, [threadId]);
  const model = chosenModel ?? inherited.model;
  // The model remains a per-turn choice. A retained live chat reuses its CLI
  // when the process-fixed settings match; changing the model makes only that
  // member process restart and native-resume inside the same sandbox.
  useEffect(() => {
    let cancelled = false;
    getAgentConfig()
      .then((loaded) => {
        if (cancelled) return;
        setConfig(loaded);
        setModels(loaded.models ?? []);
      })
      .catch(() => {
        // A list we could not load is not a list to pick from: the composer
        // falls back to naming the inherited model, which is what a turn with
        // no override runs on anyway.
        if (!cancelled) {
          setConfig(null);
          setModels([]);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // The parent's model leads even when this deployment has stopped offering
  // it, so the control cannot show one model while the turn would run another.
  const modelOptions = useMemo(() => {
    if (models.length === 0) return [];
    return composerModels(models, inherited.model);
  }, [inherited.model, models]);

  // Against the model this turn would run — the switched-to one, or the
  // inherited one — because a pair offered last turn need not be offered now.
  const efforts = useMemo(
    () => offeredEfforts(config, job.runtime, model),
    [config, job.runtime, model],
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
    model,
    setChosenModel,
    effort,
    setChosenEffort,
    modelOptions,
    efforts,
  };
}
