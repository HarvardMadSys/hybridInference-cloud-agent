'use client';

import { DEFAULT_SKILL_LIMITS } from '@/lib/skillLimits';

import { getErrorMessage } from '@/lib/utils/errors';

import { useState } from 'react';
import {
  createSkill,
  importGitHubSkill,
  previewSkillImport,
  type SkillApi,
  type SkillImportPreviewApi,
} from '@/lib/api/skills';
import {
  SkillDefault,
  SkillDialog,
  SkillErrorMessage,
  SKILL_BUTTON,
  SKILL_FIELD,
  SKILL_PRIMARY,
} from './SkillDialog';

export function SkillImportDialog({
  onClose,
  onSaved,
}: {
  onClose: () => void;
  onSaved: (skill: SkillApi) => void;
}) {
  const [mode, setMode] = useState<'github' | 'upload'>('github');
  const [url, setUrl] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<SkillImportPreviewApi | null>(null);
  const [name, setName] = useState('');
  const [defaultEnabled, setDefaultEnabled] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function review() {
    if (busy) return;
    setError(null);
    setBusy(true);
    try {
      let input: { url: string } | { content: string };
      if (mode === 'github') {
        if (!url.trim()) throw new Error('Enter a GitHub URL.');
        input = { url: url.trim() };
      } else {
        if (!file) throw new Error('Choose a SKILL.md file.');
        if (file.size > DEFAULT_SKILL_LIMITS.max_skill_bytes)
          throw new Error('SKILL.md must be no larger than 64 KB.');
        input = { content: await file.text() };
      }
      const result = await previewSkillImport(input);
      setPreview(result);
      setName(result.name);
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    if (!preview || busy) return;
    setBusy(true);
    setError(null);
    try {
      let result: SkillApi;
      if (preview.source === 'github' && preview.source_url && preview.content_digest) {
        result = await importGitHubSkill({
          url: preview.source_url,
          content_digest: preview.content_digest,
          name: name.trim(),
          default_enabled: defaultEnabled,
        });
      } else {
        result = await createSkill({
          name: name.trim(),
          description: preview.description,
          instructions: preview.instructions,
          source: 'uploaded',
          default_enabled: defaultEnabled,
        });
      }
      onSaved(result);
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <SkillDialog
      title={preview ? 'Review skill' : 'Import skill'}
      description={
        preview
          ? 'Review the instructions before adding this skill.'
          : 'Add instructions from GitHub or a SKILL.md file.'
      }
      busy={busy}
      onClose={onClose}
      footer={
        <>
          {preview ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setPreview(null);
                setError(null);
              }}
              className={`${SKILL_BUTTON} mr-auto`}
            >
              Back
            </button>
          ) : null}
          <button type="button" disabled={busy} onClick={onClose} className={SKILL_BUTTON}>
            Cancel
          </button>
          <button
            type="button"
            disabled={busy || (!!preview && !name.trim())}
            onClick={() => void (preview ? confirm() : review())}
            className={SKILL_PRIMARY}
          >
            {busy
              ? preview
                ? 'Importing…'
                : 'Reading…'
              : preview
                ? 'Import skill'
                : 'Preview skill'}
          </button>
        </>
      }
    >
      {preview ? (
        <>
          <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
            Name
            <input
              value={name}
              onChange={(event) => setName(event.target.value)}
              maxLength={DEFAULT_SKILL_LIMITS.max_name_length}
              disabled={busy}
              className={SKILL_FIELD}
            />
          </label>
          <p className="mt-3 whitespace-pre-wrap text-sm text-gray-500 dark:text-gray-400">
            {preview.description}
          </p>
          {preview.source_url ? (
            <a
              href={preview.source_url}
              target="_blank"
              rel="noreferrer"
              className="mt-2 block break-all text-xs text-gray-500 underline dark:text-gray-400"
            >
              {preview.source_url}
            </a>
          ) : null}
          <div className="mt-5 flex justify-between text-xs text-gray-500 dark:text-gray-400">
            <span>SKILL.md</span>
            {preview.source_revision ? (
              <span>Revision {preview.source_revision.slice(0, 7)}</span>
            ) : null}
          </div>
          <pre className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap break-words rounded-xl border border-gray-200 bg-gray-50 p-4 text-xs leading-relaxed text-gray-800 dark:border-white/10 dark:bg-gray-950 dark:text-gray-200">
            {preview.instructions}
          </pre>
          <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">
            Imports the instructions in SKILL.md. Referenced files and scripts are not included.
          </p>
          <SkillDefault checked={defaultEnabled} onChange={setDefaultEnabled} disabled={busy} />
        </>
      ) : (
        <>
          <div
            className="mb-5 flex gap-5 border-b border-gray-200 dark:border-white/10"
            role="group"
            aria-label="Import source"
          >
            {(['github', 'upload'] as const).map((value) => (
              <button
                type="button"
                key={value}
                disabled={busy}
                aria-pressed={mode === value}
                onClick={() => {
                  setMode(value);
                  setError(null);
                }}
                className={`border-b-2 pb-3 text-sm ${mode === value ? 'border-gray-900 font-medium text-gray-900 dark:border-gray-100 dark:text-gray-100' : 'border-transparent text-gray-500 dark:text-gray-400'}`}
              >
                {value === 'github' ? 'GitHub' : 'Upload SKILL.md'}
              </button>
            ))}
          </div>
          {mode === 'github' ? (
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
              GitHub URL
              <input
                type="url"
                value={url}
                onChange={(event) => setUrl(event.target.value)}
                disabled={busy}
                placeholder="https://github.com/your-org/skills/tree/main/code-review"
                className={SKILL_FIELD}
              />
              <span className="mt-2 block text-xs font-normal text-gray-500 dark:text-gray-400">
                Link to a skill folder or SKILL.md. Private repositories need access through your
                connected GitHub account.
              </span>
            </label>
          ) : (
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
              Skill file
              <input
                type="file"
                accept=".md,text/markdown,text/plain"
                disabled={busy}
                onChange={(event) => setFile(event.target.files?.[0] ?? null)}
                className={SKILL_FIELD}
              />
              <span className="mt-2 block text-xs font-normal text-gray-500 dark:text-gray-400">
                Up to 64 KB. Include YAML frontmatter with name and description, followed by
                Markdown instructions.
              </span>
            </label>
          )}
        </>
      )}
      <SkillErrorMessage message={error} />
    </SkillDialog>
  );
}
