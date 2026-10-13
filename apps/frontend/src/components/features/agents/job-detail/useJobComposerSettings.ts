'use client';

import { getAgentConfig, type AgentConfigApi } from '@/lib/api/agents';
import { useEffect, useMemo, useState } from 'react';
import { composerModels } from '../ConversationComposer';
import { highestEffort, offeredEfforts } from '../effort';
import type { AgentJob } from '../types';

/** Load deployment options while retaining the turn's inherited model and effort. */
export function useJobComposerSettings(job: AgentJob) {
  const [config, setConfig] = useState<AgentConfigApi | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [model, setModel] = useState(job.model);
  const [effort, setEffort] = useState(job.reasoningEffort ?? '');
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
    return composerModels(models, job.model);
  }, [job.model, models]);

  // Against the model this turn would run — the switched-to one, or the
  // inherited one — because a pair offered last turn need not be offered now.
  const efforts = useMemo(
    () => offeredEfforts(config, job.runtime, model || job.model),
    [config, job.runtime, job.model, model],
  );
  useEffect(() => {
    // Only once the config has arrived. Before that the offered set is empty
    // because nothing is *known*, not because nothing is offered — and acting
    // on it would wipe the inherited level a moment before the answer lands.
    if (!config) return;
    // The inherited level leads; a turn that inherited nothing, or one whose
    // switched-to model does not offer what it inherited, falls to the deepest
    // the new pair has.
    if (!efforts.includes(effort)) setEffort(highestEffort(efforts));
  }, [config, efforts, effort]);

  return { config, model, setModel, effort, setEffort, modelOptions, efforts };
}
