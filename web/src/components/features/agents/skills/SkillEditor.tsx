'use client';

import { DEFAULT_SKILL_LIMITS } from '@/lib/skillLimits';

import { getErrorMessage } from '@/lib/utils/errors';

import { useId, useState, type FormEvent } from 'react';
import { createSkill, updateSkill, type SkillApi } from '@/lib/api/skills';
import {
  SkillDefault,
  SkillDialog,
  SkillErrorMessage,
  SKILL_BUTTON,
  SKILL_FIELD,
  SKILL_PRIMARY,
} from './SkillDialog';

export function SkillEditor({
  skill,
  onClose,
  onSaved,
}: {
  skill?: SkillApi;
  onClose: () => void;
  onSaved: (skill: SkillApi) => void;
}) {
  const editing = Boolean(skill);
  const formId = useId();
  const [name, setName] = useState(skill?.name ?? '');
  const [description, setDescription] = useState(skill?.description ?? '');
  const [instructions, setInstructions] = useState(skill?.instructions ?? '');
  const [defaultEnabled, setDefaultEnabled] = useState(editing ? !!skill?.default_enabled : false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setError(null);
    if (new TextEncoder().encode(instructions).length > DEFAULT_SKILL_LIMITS.max_skill_bytes) {
      setError('Skill instructions must be no larger than 64 KB.');
      return;
    }
    setBusy(true);
    const content = {
      name: name.trim(),
      description: description.trim(),
      instructions: instructions.trim(),
      default_enabled: defaultEnabled,
    };
    try {
      const result =
        editing && skill
          ? await updateSkill(skill.id, { ...content, revision: skill.revision })
          : await createSkill({ ...content, source: 'created' });
      onSaved(result);
    } catch (cause) {
      setError(getErrorMessage(cause));
    } finally {
      setBusy(false);
    }
  }

  return (
    <SkillDialog
      title={editing ? 'Edit skill' : 'Create skill'}
      description="Turn a repeatable workflow into reusable instructions."
      busy={busy}
      onClose={onClose}
      footer={
        <>
          <button type="button" className={SKILL_BUTTON} disabled={busy} onClick={onClose}>
            Cancel
          </button>
          <button type="submit" form={formId} className={SKILL_PRIMARY} disabled={busy}>
            {busy ? 'Saving…' : editing ? 'Save changes' : 'Create skill'}
          </button>
        </>
      }
    >
      <form id={formId} onSubmit={(event) => void save(event)} className="space-y-4">
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
          Name
          <input
            required
            maxLength={DEFAULT_SKILL_LIMITS.max_name_length}
            value={name}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
            placeholder="e.g. Code review"
            className={SKILL_FIELD}
          />
        </label>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
          Description
          <textarea
            required
            maxLength={DEFAULT_SKILL_LIMITS.max_description_length}
            rows={2}
            value={description}
            disabled={busy}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="When should the agent use this skill?"
            className={SKILL_FIELD}
          />
          <span className="mt-1 block text-xs font-normal text-gray-500 dark:text-gray-400">
            Help the agent understand when this skill is relevant.
          </span>
        </label>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-200">
          Instructions
          <textarea
            required
            rows={8}
            value={instructions}
            disabled={busy}
            onChange={(event) => setInstructions(event.target.value)}
            placeholder="Describe the steps, conventions, and expected output…"
            className={`${SKILL_FIELD} font-mono leading-relaxed`}
          />
          <span className="mt-1 block text-xs font-normal text-gray-500 dark:text-gray-400">
            Markdown supported · up to 64 KB.
          </span>
        </label>
        <SkillDefault checked={defaultEnabled} onChange={setDefaultEnabled} disabled={busy} />
        <SkillErrorMessage message={error} />
      </form>
    </SkillDialog>
  );
}
