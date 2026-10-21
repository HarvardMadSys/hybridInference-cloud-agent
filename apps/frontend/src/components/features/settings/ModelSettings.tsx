'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { Picker } from '@/components/features/agents/Picker';
import { getModelPreference, saveModelPreference } from '@/lib/api/modelPreference';
import type { ModelPreference } from '@/lib/api/modelPreference';

function modelStatusText(
  t: ReturnType<typeof useT>,
  status: 'loading' | 'ready' | 'error',
  pending: boolean,
  effective: string | null,
  catalogRead: boolean,
) {
  if (status === 'loading') return t('settings.model.loading');
  if (pending) return t('settings.model.saving');
  if (effective) return t('settings.model.effective', { model: effective });
  return t(catalogRead ? 'settings.model.noneApplies' : 'settings.model.noModels');
}

/** Choose a concrete default; resetting lets the server resolve it again. */
export function ModelSettings() {
  const t = useT();
  const [models, setModels] = useState<string[]>([]);
  const [preference, setPreference] = useState<ModelPreference | null>(null);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [pending, setPending] = useState(false);
  const [saveError, setSaveError] = useState(false);
  const [reload, setReload] = useState(0);
  const mounted = useRef(true);
  const request = useRef<AbortController | null>(null);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      request.current?.abort();
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    request.current = controller;
    setStatus('loading');
    setSaveError(false);
    // One read: the preference arrives with the catalog it was resolved
    // against, so the card never waits on the composer's repositories.
    void getModelPreference(controller.signal)
      .then((saved) => {
        if (!mounted.current || controller.signal.aborted) return;
        setModels(saved.models);
        setPreference(saved);
        setStatus('ready');
      })
      .catch(() => {
        // An abort is a navigation, not a failure to report.
        if (mounted.current && !controller.signal.aborted) setStatus('error');
      });
    return () => controller.abort();
  }, [reload]);

  const choose = useCallback(async (value: string | null) => {
    setPending(true);
    setSaveError(false);
    try {
      const saved = await saveModelPreference(value);
      if (!mounted.current) return;
      setModels(saved.models);
      setPreference(saved);
    } catch {
      if (mounted.current) setSaveError(true);
    } finally {
      if (mounted.current) setPending(false);
    }
  }, []);

  const effective = preference?.effective_model ?? null;
  const pinned = preference?.default_model ?? null;
  const selected = pinned ?? effective ?? '';
  // No pin is called retired on the strength of an unreadable catalog.
  const catalogRead = models.length > 0;
  const unavailable = pinned !== null && catalogRead && !models.includes(pinned);

  return (
    <section aria-labelledby="settings-model" className="ui-card mt-4 bg-surface p-4 sm:p-5">
      <div className="grid items-start gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(16rem,22rem)] lg:gap-8">
        <div>
          <h2 id="settings-model" className="text-base font-semibold text-foreground">
            {t('settings.model.title')}
          </h2>
          <p className="mt-1.5 max-w-md text-sm leading-6 text-muted">
            {t('settings.model.description')}
          </p>
        </div>

        {status === 'error' ? (
          <div role="alert" className="flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
            <span className="text-status-error">{t('settings.model.error')}</span>
            <button
              type="button"
              onClick={() => setReload((value) => value + 1)}
              className="ui-button ui-button-secondary min-h-11"
            >
              {t('settings.model.retry')}
            </button>
          </div>
        ) : (
          <div className="min-w-0">
            <Picker
              label={t('settings.model.label')}
              value={selected}
              options={models}
              optionLabels={
                unavailable
                  ? { [pinned]: t('settings.model.retired', { model: pinned }) }
                  : undefined
              }
              onChange={(model) => void choose(model)}
              disabled={status === 'loading' || pending}
              placeholder={t('settings.model.choose')}
              variant="field"
            />
            <div className="mt-2 flex items-start justify-between gap-3">
              <p aria-live="polite" className="min-w-0 text-xs leading-5 text-muted">
                {modelStatusText(t, status, pending, effective, catalogRead)}
              </p>
              {pinned !== null ? (
                <button
                  type="button"
                  onClick={() => void choose(null)}
                  disabled={status === 'loading' || pending}
                  className="ui-focus-ring -mr-1 -mt-1 shrink-0 rounded-md px-1 py-1 text-xs leading-5 text-muted underline-offset-4 enabled:hover:text-foreground enabled:hover:underline disabled:opacity-50"
                >
                  {t('settings.model.reset')}
                </button>
              ) : null}
            </div>
            {saveError ? (
              <p role="alert" className="mt-2 text-sm text-status-error">
                {t('settings.model.saveError')}
              </p>
            ) : null}
          </div>
        )}
      </div>
    </section>
  );
}
