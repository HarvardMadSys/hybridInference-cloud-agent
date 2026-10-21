import { DEFAULT_MODEL } from './settingValues';

/**
 * What the default model field adds to a plain text setting: suggestions from
 * the administrator's own catalog, and a note on what the value means for
 * users. Every other field gets `NO_HINTS`.
 */
export type FieldHints = {
  /** The datalist an input points at, when there are suggestions. */
  list?: string;
  models: string[] | null;
  note: string | null;
};

export const NO_HINTS: FieldHints = { models: null, note: null };

/**
 * The note under the default model field. Only a hint: the catalog is per user,
 * so a model this account lacks may still reach others, and nobody is stranded
 * by one they lack.
 */
function defaultModelNote(value: string, models: string[] | null): string | null {
  const model = value.trim();
  if (!model) {
    return 'Empty: every user without a model of their own picks one before their first task.';
  }
  if (models && !models.includes(model)) {
    return 'Your account is not offered this model. Users who are not offered it either are asked to pick one.';
  }
  return null;
}

export function fieldHints(
  key: string,
  id: string,
  value: string,
  models: string[] | null,
): FieldHints {
  if (key !== DEFAULT_MODEL) return NO_HINTS;
  return {
    list: models ? `${id}-models` : undefined,
    models,
    note: defaultModelNote(value, models),
  };
}

export function DefaultModelHints({
  id,
  hints,
  showInput,
  warn,
}: {
  id: string;
  hints: FieldHints;
  showInput: boolean;
  /** A note about a value is a warning; the note about an empty one is not. */
  warn: boolean;
}) {
  return (
    <>
      {showInput && hints.list && hints.models && (
        <datalist id={hints.list}>
          {hints.models.map((model) => (
            <option key={model} value={model} />
          ))}
        </datalist>
      )}
      {hints.note && (
        <p
          id={`${id}-note`}
          className={`mt-2 text-xs leading-5 ${warn ? 'text-status-warning' : 'text-muted'}`}
        >
          {hints.note}
        </p>
      )}
    </>
  );
}
