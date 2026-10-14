'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useAuth, useLocale } from '@/components/providers';
import { getMemoryProjects } from '@/lib/api/agentMemory';
import { MemoryScopePanel } from './MemoryScopePanel';
import { RefreshIcon } from './icons';

/** Per-repository memory, chosen from the repositories the user has worked in. */
export function ProjectMemorySettings() {
  const { state } = useAuth();
  const { t } = useLocale();
  const userId = state.user?.id;

  return (
    <>
      <p className="text-xs leading-relaxed text-muted">{t('settings.projectMemory.hint')}</p>
      {userId ? (
        <ProjectMemorySelector key={`user:${userId}`} />
      ) : (
        <p className="mt-3 text-sm text-muted">{t('settings.memory.signIn')}</p>
      )}
    </>
  );
}

function ProjectMemorySelector() {
  const { t } = useLocale();
  const [projects, setProjects] = useState<string[]>([]);
  const [repo, setRepo] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const request = useRef<AbortController | null>(null);
  const mounted = useRef(false);
  const selectId = useId();

  const loadProjects = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(false);
    try {
      const next = await getMemoryProjects(controller.signal);
      if (!mounted.current || controller.signal.aborted) return;
      setProjects(next);
      setRepo((current) => (next.includes(current) ? current : (next[0] ?? '')));
    } catch {
      if (mounted.current && !controller.signal.aborted) setError(true);
    } finally {
      if (mounted.current && !controller.signal.aborted) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    void loadProjects();
    return () => {
      mounted.current = false;
      request.current?.abort();
    };
  }, [loadProjects]);

  return (
    <>
      <div className="mt-3 flex items-end gap-2">
        <div className="min-w-0 flex-1">
          <label htmlFor={selectId} className="text-xs font-medium text-foreground">
            {t('settings.projectMemory.project')}
          </label>
          <select
            id={selectId}
            value={repo}
            onChange={(event) => setRepo(event.target.value)}
            disabled={loading || projects.length === 0}
            className="ui-input mt-1 block min-h-11 w-full rounded-lg"
          >
            {!projects.length ? (
              <option value="">{t('settings.projectMemory.select')}</option>
            ) : null}
            {projects.map((project) => (
              <option key={project} value={project}>
                {project}
              </option>
            ))}
          </select>
        </div>
        <button
          type="button"
          onClick={() => void loadProjects()}
          disabled={loading}
          aria-label={t('settings.projectMemory.refreshProjects')}
          title={t('settings.projectMemory.refreshProjects')}
          className="ui-icon-button ui-button-secondary min-h-11 min-w-11"
        >
          <RefreshIcon />
        </button>
      </div>
      {loading ? (
        <p role="status" className="mt-3 text-sm text-muted">
          {t('settings.projectMemory.loadingProjects')}
        </p>
      ) : error ? (
        <p role="alert" className="mt-3 text-sm text-status-error">
          {t('settings.projectMemory.projectsError')}
        </p>
      ) : projects.length === 0 ? (
        <p className="mt-3 text-sm text-muted">{t('settings.projectMemory.noProjects')}</p>
      ) : null}
      {repo ? <MemoryScopePanel key={repo} repo={repo} /> : null}
    </>
  );
}
