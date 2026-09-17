'use client';

import { getErrorMessage } from '@/lib/utils/errors';
import { useCallback, useEffect, useRef, useState } from 'react';
import { listSkills, type SkillSummaryApi } from '@/lib/api/skills';

export function useSkillLibrary() {
  const [skills, setSkills] = useState<SkillSummaryApi[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const reload = useCallback(async () => {
    const request = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const rows = await listSkills();
      if (generation.current === request) setSkills(rows);
    } catch (cause) {
      if (generation.current === request) setError(getErrorMessage(cause));
    } finally {
      if (generation.current === request) setLoading(false);
    }
  }, []);
  useEffect(() => {
    void reload();
    return () => {
      generation.current += 1;
    };
  }, [reload]);
  return { skills, setSkills, loading, error, reload };
}
