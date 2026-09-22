import type { ReactNode } from 'react';
import type { TranslationKey } from '@/lib/i18n/dictionaries/en';
import { GoalIcon } from './ComposerPlusMenu';

// Slash commands, and the one rule that decides what is one.
//
// A command is a *whole* first token, not a word that happens to appear: the
// composer types prose into the same box, and a rule that matched anywhere
// would silently eat messages that mention `/goal` in a sentence or show one
// in a code block. Everything here is pure so the rule can be tested without a
// browser, because that rule is the feature's sharpest edge.

/**
 * One command the composer offers above its box.
 *
 * A table rather than a single branch: the menu is drawn from it, so a second
 * command is a second row and not a second thing that knows what a command is.
 */
export interface ComposerCommand {
  /** The token after the slash, and what the menu filters on. */
  name: string;
  /** The row's label. */
  labelKey: TranslationKey;
  /** The line beside it, saying what the command does. */
  detailKey: TranslationKey;
  /** The row's own mark, so the table is still the only place a command lives. */
  icon: ReactNode;
}

export const COMPOSER_COMMANDS: readonly ComposerCommand[] = [
  {
    name: 'goal',
    labelKey: 'commands.goal',
    detailKey: 'commands.goalHint',
    icon: <GoalIcon className="h-[17px] w-[17px] shrink-0" />,
  },
];

/**
 * The partial name being typed, while a command is still being chosen.
 *
 * Only while the whole input is the slash and a name fragment. The moment a
 * space lands the choice is made and the rest of the line is its argument, so
 * the menu stands down and the box is prose again. `/goals` is a name no
 * command has and matches nothing — which is what keeps a sentence that merely
 * starts with a slash out of the menu.
 */
export function composerCommandQuery(text: string): string | null {
  const match = /^\s*\/([^\s/]*)$/.exec(text);
  return match ? match[1].toLowerCase() : null;
}

/** The command rows the menu offers for what has been typed so far. */
export function composerCommandMatches(text: string): ComposerCommand[] {
  const query = composerCommandQuery(text);
  if (query === null) return [];
  return COMPOSER_COMMANDS.filter((command) => command.name.startsWith(query));
}

/** What a typed `/goal` holds: an objective, or nothing after the token. */
export type GoalCommand = { kind: 'set'; objective: string } | { kind: 'empty' };

/**
 * The goal command, when this input *is* one.
 *
 * Only at the very start and only as a whole token. `/goals`, `run /goal`, a
 * line inside a fenced block, and `/goal` with anything but whitespace glued to
 * it are all ordinary text, and are left for the model. Everything after the
 * token is the objective, because an objective is prose and may run to several
 * lines.
 *
 * A bare `/goal` is `empty`, and that is not an objective: the composer asks
 * for the rest of the sentence rather than creating a goal with no content or
 * sending the token to the model as prose.
 */
export function parseGoalCommand(text: string): GoalCommand | null {
  const match = /^\s*\/goal(?=\s|$)([\s\S]*)$/i.exec(text);
  if (!match) return null;
  const objective = match[1].trim();
  return objective ? { kind: 'set', objective } : { kind: 'empty' };
}
