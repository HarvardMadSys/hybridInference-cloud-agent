'use client';

import { getErrorMessage } from '@/lib/utils/errors';

import { useEffect, useState } from 'react';
import {
  deleteSkill,
  getSkill,
  updateSkill,
  type SkillApi,
  type SkillSummaryApi,
} from '@/lib/api/skills';
import {
  SkillDefault,
  SkillDialog,
  SkillErrorMessage,
  SkillIcon,
  SKILL_BUTTON,
  SKILL_PRIMARY,
} from './SkillDialog';
import { SkillEditor } from './SkillEditor';
import { SkillImportDialog } from './SkillImportDialog';
import { useSkillLibrary } from './useSkillLibrary';
import { SkillRow, SOURCE_LABELS, type SkillAction } from './SkillRow';

function SkillDetails({
  summary,
  action = 'view',
  onSaved,
  onClose,
  onEdit,
  onChanged,
  onRemoved,
}: {
  summary: SkillSummaryApi;
  action?: SkillAction;
  onSaved: (skill: SkillApi) => void;
  onClose: () => void;
  onEdit: (skill: SkillApi) => void;
  onChanged: (skill: SkillApi) => void;
  onRemoved: (id: number) => void;
}) {
  const [skill, setSkill] = useState<SkillApi | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState(action === 'remove');
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let current = true;
    setLoading(true);
    setError(null);
    getSkill(summary.id)
      .then((result) => {
        if (current) setSkill(result);
      })
      .catch((cause) => {
        if (current) setError(getErrorMessage(cause));
      })
      .finally(() => {
        if (current) setLoading(false);
      });
    return () => {
      current = false;
    };
  }, [summary.id, retry]);

  async function changeDefault(value: boolean) {
    if (!skill || busy) return;
    setBusy(true);
    setError(null);
    try {
      const updated = await updateSkill(skill.id, {
        revision: skill.revision,
        default_enabled: value,
      });
      setSkill(updated);
      onChanged(updated);
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    if (!skill || busy) return;
    setBusy(true);
    setError(null);
    try {
      await deleteSkill(skill.id, skill.revision);
      onRemoved(skill.id);
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  if (skill && action === 'edit') {
    return <SkillEditor skill={skill} onClose={onClose} onSaved={onSaved} />;
  }

  return (
    <SkillDialog
      title={skill?.name ?? summary.name}
      description={skill?.description ?? summary.description}
      busy={busy}
      onClose={onClose}
      footer={
        skill && !loading ? (
          removing ? (
            <>
              <p className="w-full pb-2 text-sm text-gray-600 dark:text-gray-300">
                Remove this skill from your library? Existing conversations keep their saved
                version.
              </p>
              <button
                type="button"
                disabled={busy}
                onClick={() => setRemoving(false)}
                className={SKILL_BUTTON}
              >
                Cancel
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => void remove()}
                className="rounded-lg bg-red-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-red-500"
              >
                {busy ? 'Removing…' : 'Confirm removal'}
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                disabled={busy}
                onClick={() => setRemoving(true)}
                className="mr-auto text-sm text-red-600 disabled:opacity-50 dark:text-red-400"
              >
                Remove skill
              </button>
              {skill.source !== 'github' ? (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => onEdit(skill)}
                  className={SKILL_BUTTON}
                >
                  Edit skill
                </button>
              ) : null}
              <button type="button" disabled={busy} onClick={onClose} className={SKILL_PRIMARY}>
                Done
              </button>
            </>
          )
        ) : undefined
      }
    >
      {loading ? (
        <p role="status" className="text-sm text-gray-500 dark:text-gray-400">
          Loading skill…
        </p>
      ) : skill ? (
        <>
          <div className="flex flex-wrap items-center gap-2 text-xs text-gray-500 dark:text-gray-400">
            <span>{SOURCE_LABELS[skill.source]}</span>
            <span>· Version {skill.content_revision ?? skill.revision}</span>
            {skill.source_revision ? <span>· {skill.source_revision.slice(0, 7)}</span> : null}
          </div>
          {skill.source_url ? (
            <a
              href={skill.source_url}
              target="_blank"
              rel="noreferrer"
              className="mt-2 block break-all text-xs text-gray-500 underline dark:text-gray-400"
            >
              {skill.source_url}
            </a>
          ) : null}
          <h3 className="mt-5 text-xs font-medium text-gray-500 dark:text-gray-400">
            Instructions
          </h3>
          <pre className="mt-2 max-h-80 overflow-y-auto whitespace-pre-wrap break-words rounded-xl border border-gray-200 bg-gray-50 p-4 text-xs leading-relaxed text-gray-800 dark:border-white/10 dark:bg-gray-950 dark:text-gray-200">
            {skill.instructions}
          </pre>
          <SkillDefault
            checked={skill.default_enabled}
            onChange={(value) => void changeDefault(value)}
            disabled={busy || removing}
          />
        </>
      ) : null}
      <SkillErrorMessage message={error} />
      {error ? (
        <button
          type="button"
          onClick={() => {
            setRemoving(false);
            setRetry((value) => value + 1);
          }}
          disabled={busy}
          className={`${SKILL_BUTTON} mt-3`}
        >
          Refresh skill
        </button>
      ) : null}
    </SkillDialog>
  );
}

type DialogState =
  | { kind: 'create'; skill?: SkillApi }
  | { kind: 'import' }
  | { kind: 'detail'; skill: SkillSummaryApi; action?: SkillAction }
  | null;

/** Personal instruction library, directly below MCP on the Integrations page. */
export function SkillsSection() {
  const { skills, setSkills, loading, error, reload } = useSkillLibrary();
  const [dialog, setDialog] = useState<DialogState>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [mutationError, setMutationError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  function changed(skill: SkillApi) {
    setSkills((rows) =>
      [...rows.filter((row) => row.id !== skill.id), skill].sort((a, b) =>
        a.name.localeCompare(b.name),
      ),
    );
  }
  function saved(skill: SkillApi) {
    changed(skill);
    setDialog(null);
    setMutationError(null);
    setNotice(`${skill.name} saved to your skills.`);
  }
  async function toggle(skill: SkillSummaryApi) {
    if (busyId !== null) return;
    setBusyId(skill.id);
    setMutationError(null);
    setNotice(null);
    try {
      changed(
        await updateSkill(skill.id, {
          revision: skill.revision,
          default_enabled: !skill.default_enabled,
        }),
      );
      setNotice('Defaults updated for new chats.');
    } catch (cause) {
      setMutationError(getErrorMessage(cause));
      void reload();
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section
      aria-labelledby="skills-heading"
      className="mt-10 rounded-2xl border border-gray-200 bg-white px-6 pt-6 pb-2 shadow-sm dark:border-white/10 dark:bg-gray-900"
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 id="skills-heading" className="font-medium text-gray-900 dark:text-gray-50">
            Skills
          </h2>
          <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Save reusable instructions for your agents.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => setDialog({ kind: 'import' })}
            disabled={loading}
            className={SKILL_BUTTON}
          >
            Import skill
          </button>
          <button
            type="button"
            onClick={() => setDialog({ kind: 'create' })}
            disabled={loading}
            className={SKILL_PRIMARY}
          >
            <span aria-hidden="true">+</span>Create skill
          </button>
        </div>
      </div>
      {notice ? (
        <p role="status" className="mt-4 text-sm text-emerald-700 dark:text-emerald-400">
          {notice}
        </p>
      ) : null}
      <SkillErrorMessage message={mutationError ?? error} />
      {error ? (
        <button type="button" onClick={() => void reload()} className={`${SKILL_BUTTON} mt-3 mb-5`}>
          Try again
        </button>
      ) : null}
      {loading ? (
        <p role="status" className="py-10 text-center text-sm text-gray-500 dark:text-gray-400">
          Loading skills…
        </p>
      ) : !error && skills.length ? (
        <>
          <div className="mt-6 flex items-center justify-between border-b border-gray-100 pb-2 text-xs text-gray-500 dark:border-white/5 dark:text-gray-400">
            <span>Your skills</span>
            <span
              className="mr-10 w-16 text-center sm:mr-11"
              title="Automatically enable skills in new chats"
            >
              Auto-use
            </span>
          </div>
          <div className="divide-y divide-gray-100 dark:divide-white/5">
            {skills.map((skill) => (
              <SkillRow
                key={skill.id}
                skill={skill}
                disabled={busyId !== null}
                onToggle={() => void toggle(skill)}
                onAction={(action) => setDialog({ kind: 'detail', skill, action })}
              />
            ))}
          </div>
        </>
      ) : !error ? (
        <div className="py-12 text-center">
          <span className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-xl bg-gray-100 text-gray-400 dark:bg-white/5 dark:text-gray-500">
            <SkillIcon className="h-6 w-6" />
          </span>
          <h3 className="text-sm font-medium text-gray-800 dark:text-gray-200">
            Your workflows, ready to reuse
          </h3>
          <p className="mt-2 text-sm text-gray-500 dark:text-gray-400">
            Import a skill or create your own instructions.
          </p>
        </div>
      ) : null}
      {dialog?.kind === 'create' ? (
        <SkillEditor skill={dialog.skill} onClose={() => setDialog(null)} onSaved={saved} />
      ) : null}
      {dialog?.kind === 'import' ? (
        <SkillImportDialog onClose={() => setDialog(null)} onSaved={saved} />
      ) : null}
      {dialog?.kind === 'detail' ? (
        <SkillDetails
          summary={dialog.skill}
          action={dialog.action}
          onSaved={saved}
          onClose={() => setDialog(null)}
          onEdit={(skill) => setDialog({ kind: 'create', skill })}
          onChanged={changed}
          onRemoved={(id) => {
            setSkills((rows) => rows.filter((row) => row.id !== id));
            setDialog(null);
            setNotice('Skill removed from your library.');
          }}
        />
      ) : null}
    </section>
  );
}
