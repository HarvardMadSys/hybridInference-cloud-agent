'use client';

import { useCallback, useEffect, useId, useRef, useState } from 'react';
import { useT } from '@/components/providers';
import { getModelPreference, saveModelPreference } from '@/lib/api/modelPreference';
import type { ModelPreference } from '@/lib/api/modelPreference';

/**
 * The sentinel option that means "follow my last model".
 *
 * A native `<select>` cannot carry a null value, and the empty string is not a
 * model. Using a value no model can be avoids the two being confused — a model
 * genuinely named `__follow__` is refused by the gateway, not silently treated
 * as a request to clear the pin.
 */
const FOLLOW_LAST = '__follow_last__';

/**
 * Pick the model a new task starts on.
 *
 * Two controls, and they are not the same thing: the select pins a model (or
 * follows the last used one), and the card says what that currently resolves
 * to. Without the second sentence a retired pin looks like it is working while
 * every new task quietly starts somewhere else.
 */
export function ModelSettings() {
  const t = useT();
  const selectId = useId();
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

  const choose = useCallback(async (next: string) => {
    const value = next === FOLLOW_LAST ? null : next;
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
  const selected = pinned ?? FOLLOW_LAST;
  // An empty catalog is an unreadable one, not a deployment that retired
  // everything: nothing is called unavailable on the strength of it.
  const catalogRead = models.length > 0;
  // A pin missing from the list would otherwise make the select silently show
  // "follow my last model" while the owner's saved choice is something else
  // entirely. It is kept as an option — labelled unavailable only when the
  // catalog actually says so — so the control shows what is saved and the
  // sentence below says what is actually applied.
  const unlistedPin = pinned !== null && !models.includes(pinned) ? pinned : null;
  const last = preference?.last_model ?? null;
  const fallback = preference?.deployment_default ?? null;
  // "Follow my last model" names where it currently lands, which is not always
  // the last model: a retired one is skipped for the deployment default, and a
  // user who has never created a task has no other way to learn what their
  // first one will use.
  const followDescription =
    last && (models.includes(last) || !catalogRead)
      ? t('settings.model.followDescription', { model: last })
      : last && fallback
        ? t('settings.model.followRetiredDeployment', { last, model: fallback })
        : last
          ? t('settings.model.followRetired', { last })
          : fallback
            ? t('settings.model.followDeployment', { model: fallback })
            : t('settings.model.followDescriptionEmpty');

  return (
    <section aria-labelledby="settings-model" className="ui-card mt-4 bg-surface p-3 sm:p-4">
      <h2 id="settings-model" className="text-base font-semibold text-foreground">
        {t('settings.model.title')}
      </h2>
      <p className="mt-2 text-sm leading-6 text-muted">{t('settings.model.description')}</p>

      {status === 'error' ? (
        <div role="alert" className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2 text-sm">
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
        <>
          <label htmlFor={selectId} className="mt-3 block text-sm font-medium text-foreground">
            {t('settings.model.label')}
          </label>
          <select
            id={selectId}
            value={selected}
            onChange={(event) => void choose(event.target.value)}
            disabled={status === 'loading' || pending}
            className="ui-input mt-1 block min-h-11 w-full max-w-sm rounded-lg"
          >
            <option value={FOLLOW_LAST}>{followDescription}</option>
            {unlistedPin ? (
              <option value={unlistedPin}>
                {catalogRead ? t('settings.model.retired', { model: unlistedPin }) : unlistedPin}
              </option>
            ) : null}
            {models.map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
          </select>

          <p aria-live="polite" className="mt-2 text-sm text-muted">
            {status === 'loading'
              ? t('settings.model.loading')
              : effective
                ? t('settings.model.effective', { model: effective })
                : catalogRead
                  ? t('settings.model.noneApplies')
                  : t('settings.model.noModels')}
          </p>
          {saveError ? (
            <p role="alert" className="mt-2 text-sm text-status-error">
              {t('settings.model.saveError')}
            </p>
          ) : null}
        </>
      )}
    </section>
  );
}
