// @vitest-environment jsdom
import { describe, expect, it } from 'vitest';

import { composerCommandMatches, composerCommandQuery, parseGoalCommand } from './composerCommands';

// The composer takes prose and commands in the same box, so the rule that tells
// them apart is the whole feature. Every case here is a message someone could
// plausibly type and would be furious to lose.

describe('parseGoalCommand', () => {
  it('recognises the bare command, with or without an argument', () => {
    // `empty` is not an objective. The composer consumes it and asks for the
    // rest of the sentence rather than creating a goal with no content or
    // sending the token to the model as prose.
    expect(parseGoalCommand('/goal')).toEqual({ kind: 'empty' });
    expect(parseGoalCommand('/goal   ')).toEqual({ kind: 'empty' });
    // Leading whitespace is still the start of the input: refusing it would be
    // a rule about a stray space rather than about what the input is.
    expect(parseGoalCommand('  /goal')).toEqual({ kind: 'empty' });
    expect(parseGoalCommand('/goal fix the flaky test')).toEqual({
      kind: 'set',
      objective: 'fix the flaky test',
    });
  });

  it('takes the whole remainder as the objective, newlines included', () => {
    // An objective is prose, and a second paragraph is part of what done means.
    expect(parseGoalCommand('/goal  ship it\n\nand leave the tree clean')).toEqual({
      kind: 'set',
      objective: 'ship it\n\nand leave the tree clean',
    });
  });

  it('accepts the command in either case', () => {
    expect(parseGoalCommand('/Goal ship it')).toEqual({ kind: 'set', objective: 'ship it' });
  });

  it('does not match a longer word that merely starts the same way', () => {
    // `/goals` is a name no command has. Reading it as `/goal` would swallow a
    // message about goals.
    expect(parseGoalCommand('/goals are tracked per conversation')).toBeNull();
    expect(parseGoalCommand('/goalfish')).toBeNull();
    expect(parseGoalCommand('/ goal of this task')).toBeNull();
  });

  it('does not match the command anywhere but the start of the input', () => {
    expect(parseGoalCommand('please run /goal when you are done')).toBeNull();
    expect(parseGoalCommand('first line\n/goal second line')).toBeNull();
    // A quoted or fenced example is text the owner is *showing* the model, not
    // an instruction to the platform.
    expect(parseGoalCommand('```\n/goal make pytest pass\n```')).toBeNull();
    expect(parseGoalCommand('> /goal make pytest pass')).toBeNull();
  });

  it('is nothing at all when there is no command', () => {
    expect(parseGoalCommand('')).toBeNull();
    expect(parseGoalCommand('Fix the flaky test')).toBeNull();
    expect(parseGoalCommand('/help')).toBeNull();
  });
});

describe('composerCommandMatches', () => {
  it('offers the command from the first slash, and narrows as it is typed', () => {
    for (const typed of ['/', '/g', '/go', '/goa', '/goal', '/GOAL']) {
      expect(composerCommandMatches(typed).map((command) => command.name)).toEqual(['goal']);
    }
  });

  it('stands down once the command has been chosen', () => {
    // A space ends the choice and starts the argument: the box is prose now,
    // and the menu has nothing left to offer.
    expect(composerCommandMatches('/goal ')).toEqual([]);
    expect(composerCommandMatches('/goal fix the flaky test')).toEqual([]);
  });

  it('offers nothing for a name no command has', () => {
    expect(composerCommandMatches('/goals')).toEqual([]);
    expect(composerCommandMatches('/x')).toEqual([]);
  });

  it('offers nothing when the slash is not the whole input', () => {
    // `/` in the middle of a sentence is a path, a division, or an address.
    expect(composerCommandMatches('run /')).toEqual([]);
    expect(composerCommandMatches('src/lib/api/')).toEqual([]);
    expect(composerCommandMatches('')).toEqual([]);
  });

  it('reads a partial name without its case', () => {
    expect(composerCommandQuery('  /Go')).toBe('go');
    expect(composerCommandQuery('/goal now')).toBeNull();
  });
});
