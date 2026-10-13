'use client';

import { useEffect, useRef, useState } from 'react';
import { useAuth, useT } from '@/components/providers';
import { getUserProfile, saveUserProfile, type UserProfile } from '@/lib/api/userProfile';
import { APIError } from '@/lib/utils/errors';

const MAX_IMAGE_BYTES = 2 * 1024 * 1024;
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);
const ROW =
  'grid gap-3 border-b border-border py-5 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] md:items-center md:gap-6';
const INPUT =
  'ui-focus-ring w-full min-w-0 rounded-lg border border-border bg-surface px-3 py-2 text-sm text-foreground disabled:opacity-50';

export function ProfileSettings() {
  const { state } = useAuth();
  const t = useT();
  return (
    <section aria-labelledby="profile-heading" className="mt-6">
      <h2 id="profile-heading" className="mb-3 text-base font-medium text-foreground">
        {t('settings.profile.title')}
      </h2>
      {state.user?.id ? (
        <ProfileEditor key={state.user.id} />
      ) : (
        <p className="text-sm text-muted">{t('settings.profile.signIn')}</p>
      )}
    </section>
  );
}

function ProfileEditor() {
  const t = useT();
  const [saved, setSaved] = useState<UserProfile | null>(null);
  const [firstName, setFirstName] = useState('');
  const [lastName, setLastName] = useState('');
  const [image, setImage] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [readingImage, setReadingImage] = useState(false);
  const [reload, setReload] = useState(0);
  const [error, setError] = useState<'load' | 'save' | 'conflict' | null>(null);
  const [imageError, setImageError] = useState<'type' | 'size' | 'read' | null>(null);
  const [notice, setNotice] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const reader = useRef<FileReader | null>(null);
  const savingRequest = useRef<AbortController | null>(null);

  const applyProfile = (profile: UserProfile) => {
    setSaved(profile);
    setFirstName(profile.first_name);
    setLastName(profile.last_name);
    setImage(profile.image_data_url);
  };

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    setImageError(null);
    setNotice(false);
    void getUserProfile(controller.signal)
      .then((profile) => {
        if (!controller.signal.aborted) applyProfile(profile);
      })
      .catch(() => {
        if (!controller.signal.aborted) setError('load');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [reload]);

  useEffect(
    () => () => {
      savingRequest.current?.abort();
      reader.current?.abort();
      reader.current = null;
    },
    [],
  );

  const chooseImage = (file?: File) => {
    if (!file) return;
    reader.current?.abort();
    reader.current = null;
    setReadingImage(false);
    setImageError(null);
    setNotice(false);
    if (!IMAGE_TYPES.has(file.type)) {
      setImageError('type');
      return;
    }
    if (!file.size || file.size > MAX_IMAGE_BYTES) {
      setImageError('size');
      return;
    }
    const next = new FileReader();
    reader.current = next;
    setReadingImage(true);
    next.onload = () => {
      if (reader.current !== next) return;
      if (typeof next.result === 'string') setImage(next.result);
      else setImageError('read');
      setReadingImage(false);
      reader.current = null;
    };
    next.onerror = () => {
      if (reader.current !== next) return;
      setImageError('read');
      setReadingImage(false);
      reader.current = null;
    };
    next.readAsDataURL(file);
  };

  const dirty =
    saved !== null &&
    (firstName.trim() !== saved.first_name ||
      lastName.trim() !== saved.last_name ||
      image !== saved.image_data_url);
  const disabled = loading || saving || !saved;
  const initials = [...firstName.trim()][0] ?? [...lastName.trim()][0] ?? '';

  const save = async () => {
    if (
      !saved ||
      !dirty ||
      disabled ||
      readingImage ||
      imageError ||
      error === 'conflict' ||
      savingRequest.current
    )
      return;
    const controller = new AbortController();
    savingRequest.current = controller;
    setSaving(true);
    setError(null);
    setNotice(false);
    try {
      const profile = await saveUserProfile(
        {
          first_name: firstName.trim(),
          last_name: lastName.trim(),
          expected_revision: saved.revision,
          ...(image !== saved.image_data_url ? { image_data_url: image } : {}),
        },
        controller.signal,
      );
      if (!controller.signal.aborted) {
        applyProfile(profile);
        setNotice(true);
      }
    } catch (cause) {
      if (!controller.signal.aborted) {
        if (cause instanceof APIError && cause.statusCode === 409) setError('conflict');
        else if (
          cause instanceof APIError &&
          cause.statusCode === 422 &&
          image !== saved.image_data_url
        )
          setImageError('read');
        else setError('save');
      }
    } finally {
      if (!controller.signal.aborted) {
        setSaving(false);
        savingRequest.current = null;
      }
    }
  };

  return (
    <form
      className="rounded-2xl border border-border bg-surface px-4 sm:px-6"
      aria-busy={loading || saving}
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
    >
      <div className={ROW}>
        <span id="profile-email-label" className="text-sm text-foreground">
          {t('settings.profile.email')}
        </span>
        <p aria-labelledby="profile-email-label" className="min-w-0 break-all text-sm text-muted">
          {loading ? t('settings.profile.loading') : (saved?.email ?? '—')}
        </p>
      </div>
      <div className={ROW}>
        <span className="text-sm text-foreground">{t('settings.profile.image')}</span>
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <div className="flex h-14 w-14 shrink-0 items-center justify-center overflow-hidden rounded-full bg-surface-muted text-lg text-foreground">
            {image ? (
              // Only selected raster files and server-sanitized data URLs are displayed.
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={image}
                alt={t('settings.profile.imageAlt')}
                className="h-full w-full object-cover"
              />
            ) : initials ? (
              <span aria-hidden="true">{initials}</span>
            ) : (
              <svg
                aria-hidden="true"
                className="h-6 w-6 text-muted"
                fill="none"
                viewBox="0 0 24 24"
                stroke="currentColor"
                strokeWidth={1.5}
              >
                <circle cx="12" cy="8" r="4" />
                <path d="M4 21v-2a8 8 0 0 1 16 0v2" />
              </svg>
            )}
          </div>
          <input
            ref={input}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            className="hidden"
            tabIndex={-1}
            aria-label={t('settings.profile.upload')}
            aria-describedby="profile-image-help"
            disabled={disabled}
            onChange={(event) => {
              chooseImage(event.target.files?.[0]);
              event.target.value = '';
            }}
          />
          <button
            type="button"
            className="ui-button ui-button-secondary ui-focus-ring shrink-0 rounded-lg"
            disabled={disabled || readingImage}
            onClick={() => input.current?.click()}
          >
            {readingImage ? t('settings.profile.reading') : t('settings.profile.upload')}
          </button>
          {image ? (
            <button
              type="button"
              className="ui-focus-ring rounded px-1 py-2 text-sm text-muted hover:text-foreground"
              disabled={disabled || readingImage}
              onClick={() => {
                setImage(null);
                setImageError(null);
                setNotice(false);
              }}
            >
              {t('settings.profile.remove')}
            </button>
          ) : null}
          <p id="profile-image-help" className="text-xs leading-5 text-muted">
            {t('settings.profile.imageHelp')}
          </p>
          {imageError ? (
            <p role="alert" className="w-full text-sm text-red-600 dark:text-red-400">
              {t(`settings.profile.imageError.${imageError}`)}
            </p>
          ) : null}
        </div>
      </div>
      <div className={ROW}>
        <label htmlFor="profile-first-name" className="text-sm text-foreground">
          {t('settings.profile.firstName')}
        </label>
        <input
          id="profile-first-name"
          name="given-name"
          autoComplete="given-name"
          maxLength={100}
          className={INPUT}
          value={firstName}
          disabled={disabled}
          onChange={(event) => {
            setFirstName(event.target.value);
            setNotice(false);
          }}
        />
      </div>
      <div className={`${ROW} border-b-0`}>
        <label htmlFor="profile-last-name" className="text-sm text-foreground">
          {t('settings.profile.lastName')}
        </label>
        <input
          id="profile-last-name"
          name="family-name"
          autoComplete="family-name"
          maxLength={100}
          className={INPUT}
          value={lastName}
          disabled={disabled}
          onChange={(event) => {
            setLastName(event.target.value);
            setNotice(false);
          }}
        />
      </div>
      <div className="flex flex-wrap items-center justify-end gap-3 pb-5 pt-2">
        {error ? (
          <div
            role="alert"
            className="mr-auto flex min-w-0 flex-wrap items-center gap-2 text-sm text-red-600 dark:text-red-400"
          >
            <span>{t(`settings.profile.error.${error}`)}</span>
            {error === 'load' || error === 'conflict' ? (
              <button
                type="button"
                className="ui-button ui-button-secondary ui-focus-ring rounded-lg"
                onClick={() => setReload((value) => value + 1)}
              >
                {error === 'load' ? t('settings.profile.retry') : t('settings.profile.reload')}
              </button>
            ) : null}
          </div>
        ) : null}
        {notice ? (
          <p role="status" className="mr-auto text-sm text-muted">
            {t('settings.profile.saved')}
          </p>
        ) : null}
        <button
          type="submit"
          className="ui-button ui-button-primary ui-focus-ring rounded-lg"
          disabled={
            disabled || !dirty || readingImage || Boolean(imageError) || error === 'conflict'
          }
        >
          {saving ? t('settings.profile.saving') : t('settings.profile.save')}
        </button>
      </div>
    </form>
  );
}
