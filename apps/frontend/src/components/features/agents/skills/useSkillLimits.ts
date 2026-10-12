import { useEffect, useState } from 'react';

import { getAgentConfig } from '@/lib/api/agents';
import { DEFAULT_SKILL_LIMITS, type SkillLimits } from '@/lib/skillLimits';

/** Read current limits whenever a skill editor or import dialog opens. */
export function useSkillLimits(): SkillLimits {
  const [limits, setLimits] = useState<SkillLimits>(DEFAULT_SKILL_LIMITS);
  useEffect(() => {
    let current = true;
    getAgentConfig()
      .then((config) => {
        if (current && config.skill_limits) setLimits(config.skill_limits);
      })
      // The server still validates submissions if the configuration request fails.
      .catch(() => {});
    return () => {
      current = false;
    };
  }, []);
  return limits;
}
