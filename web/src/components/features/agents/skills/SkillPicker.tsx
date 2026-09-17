'use client';

import { DEFAULT_SKILL_LIMITS, type SkillLimits } from '@/lib/skillLimits';

import { getErrorMessage } from '@/lib/utils/errors';

import Link from 'next/link';
import { useEffect, useState } from 'react';
import { listSkills, type SelectedSkillApi, type SkillSummaryApi } from '@/lib/api/skills';
import {
  SkillDialog,
  SkillErrorMessage,
  SkillIcon,
  SkillSwitch,
  SKILL_BUTTON,
  SKILL_PRIMARY,
} from './SkillDialog';

function SelectionDialog({
  selected,
  onApply,
  onClose,
  limits,
}: {
  limits: SkillLimits;
  selected: SelectedSkillApi[];
  onApply: (skills: SelectedSkillApi[]) => void;
  onClose: () => void;
}) {
  const [skills, setSkills] = useState<SkillSummaryApi[]>([]);
  const [checked, setChecked] = useState(() => new Set(selected.map((skill) => skill.id)));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    let current = true;
    setLoading(true);
    setError(null);
    listSkills()
      .then((rows) => {
        if (current) setSkills(rows);
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
  }, [retry]);
  const available = new Set(skills.map((skill) => skill.id));
  const missing = selected.filter((skill) => !available.has(skill.id));
  const selectedBytes = skills.reduce(
    (total, skill) => total + (checked.has(skill.id) ? (skill.instruction_bytes ?? 0) : 0),
    0,
  );
  const oversized = selectedBytes > limits.max_selection_bytes;
  const invalid = missing.some((skill) => checked.has(skill.id));
  return (
    <SkillDialog
      title="Skills"
      description="For this conversation"
      onClose={onClose}
      footer={
        <>
          <Link
            href="/integrations"
            className="mr-auto text-sm text-gray-500 underline dark:text-gray-400"
          >
            Manage skills
          </Link>
          <button type="button" onClick={onClose} className={SKILL_BUTTON}>
            Cancel
          </button>
          <button
            type="button"
            disabled={
              loading || !!error || invalid || oversized || checked.size > limits.max_selected
            }
            onClick={() => onApply(skills.filter((skill) => checked.has(skill.id)))}
            className={SKILL_PRIMARY}
          >
            Apply
          </button>
        </>
      }
    >
      {loading ? (
        <p role="status" className="py-5 text-sm text-gray-500 dark:text-gray-400">
          Loading skills…
        </p>
      ) : error ? (
        <>
          <SkillErrorMessage message={error} />
          <button
            type="button"
            onClick={() => setRetry((value) => value + 1)}
            className={`${SKILL_BUTTON} mt-3`}
          >
            Try again
          </button>
        </>
      ) : (
        <div className="divide-y divide-gray-100 dark:divide-white/5">
          {[...skills, ...missing].map((skill) => (
            <div key={skill.id} className="flex items-center justify-between gap-4 py-3">
              <div className="min-w-0">
                <span
                  className="block truncate text-sm font-medium text-gray-900 dark:text-gray-100"
                  title={skill.name}
                >
                  {skill.name}
                </span>
                {!available.has(skill.id) ? (
                  <span className="mt-0.5 block text-xs text-gray-500 dark:text-gray-400">
                    Removed from library. Turn off to continue.
                  </span>
                ) : null}
              </div>
              <SkillSwitch
                label={skill.name}
                checked={checked.has(skill.id)}
                onChange={(enabled) =>
                  setChecked((old) => {
                    const next = new Set(old);
                    if (enabled) next.add(skill.id);
                    else next.delete(skill.id);
                    return next;
                  })
                }
              />
            </div>
          ))}
          {!skills.length && !missing.length ? (
            <p className="py-5 text-sm text-gray-500 dark:text-gray-400">
              Add skills in Integrations to make them available here.
            </p>
          ) : null}
        </div>
      )}
      {checked.size > limits.max_selected ? (
        <SkillErrorMessage message={`Select at most ${limits.max_selected} skills.`} />
      ) : null}
      {oversized ? (
        <SkillErrorMessage message="Unselect a skill to fit the combined instruction size limit." />
      ) : null}
      <p className="mt-3 text-xs text-gray-500 dark:text-gray-400">Applies to your next message.</p>
    </SkillDialog>
  );
}

export function SkillPicker({
  selected,
  onChange,
  disabled = false,
  limits = DEFAULT_SKILL_LIMITS,
}: {
  selected: SelectedSkillApi[];
  onChange: (skills: SelectedSkillApi[]) => void;
  disabled?: boolean;
  limits?: SkillLimits;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen(true)}
        aria-label={`Skills for this conversation: ${selected.length} selected`}
        aria-haspopup="dialog"
        className="inline-flex shrink-0 items-center gap-1.5 rounded-md border border-gray-200 px-2 py-1 text-xs text-gray-600 hover:bg-gray-50 disabled:opacity-50 dark:border-white/10 dark:text-gray-300 dark:hover:bg-white/5"
      >
        <SkillIcon />
        Skills
        {selected.length ? (
          <span className="rounded bg-gray-100 px-1 text-[10px] dark:bg-white/10">
            {selected.length}
          </span>
        ) : null}
      </button>
      {open ? (
        <SelectionDialog
          limits={limits}
          selected={selected}
          onClose={() => setOpen(false)}
          onApply={(skills) => {
            const originalIds = new Set(selected.map((skill) => skill.id));
            if (
              skills.length !== originalIds.size ||
              skills.some((skill) => !originalIds.has(skill.id))
            ) {
              onChange(skills);
            }
            setOpen(false);
          }}
        />
      ) : null}
    </>
  );
}
