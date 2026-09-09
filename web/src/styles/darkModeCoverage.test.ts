import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

/**
 * Dark mode is only as complete as its least-swept file.
 *
 * Tailwind is configured `darkMode: 'class'`, which means a light-mode colour
 * with no `dark:` counterpart is not a subtle regression — it is white text on
 * white, or an invisible border, on whichever screen it lives on. Nothing about
 * writing `text-gray-600` warns you; the screen only breaks for people who have
 * the toggle set the other way, which is nobody on the reviewer's machine.
 *
 * So this walks the source and fails on any light-ground utility that has no
 * dark counterpart for the same property in the same class run.
 */

const SRC = resolve(__dirname, '..');

/** Utilities that are unreadable or invisible on a dark ground. */
const LIGHT_GROUND =
  /^(?:[a-z-]+:)*(?:bg|text|border|divide|ring|placeholder:text|from|via|to)-(?:white|black|gray-\d+|slate-\d+|zinc-\d+|neutral-\d+|(?:red|amber|emerald|blue|indigo|cyan|purple|rose|green|yellow)-(?:50|100|200|300|600|700|800|900)|crimson(?:-dark)?)(?:\/\[?[\d.]+\]?)?$/;

/**
 * Surfaces that are *already* dark in light mode — a code well, a log pane, an
 * inverted button. Their foreground is light on purpose, and the whole run is
 * exempt: asking for a dark variant of `text-gray-200` on `bg-gray-950` would
 * be asking to make it unreadable.
 */
const INVERTED_SURFACE =
  /(?:^|\s)bg-(?:black|gray-(?:900|950)|red-950|slate-900)(?:\/\S+)?(?=\s|$)/;

function isNeutral(token: string): boolean {
  // The brand accent and every focus indicator read on both grounds.
  if (token.includes('crimson')) return true;
  if (/^(?:focus|focus-visible|focus-within):/.test(token)) return true;
  // White below half opacity is an overlay on a coloured ground — a spinner
  // track on the accent button, a hairline on a photo — not a surface. Nobody
  // paints a panel with 15% white. The limit is deliberate: at higher opacity
  // it is a surface again, and the check applies.
  if (/^border-t-white$/.test(token)) return true;
  const overlay = /^(?:bg|border|border-t)-white\/(\d+)$/.exec(token);
  if (overlay && Number(overlay[1]) <= 50) return true;
  return ['text-white', 'bg-transparent', 'border-transparent', 'text-transparent'].includes(token);
}

/** Which property a utility governs, so `dark:` cover can be matched per property. */
function propertyOf(token: string): string {
  const parts = token.split(':');
  const base = parts[parts.length - 1];
  const variants = parts.slice(0, -1).join(':');
  const prefix = ['bg-', 'text-', 'border-', 'divide-', 'ring-', 'from-', 'via-', 'to-'].find((p) =>
    base.startsWith(p),
  );
  return `${variants}|${prefix ?? base}`;
}

/**
 * Every string that could be a class list: quoted strings (which includes the
 * branches of a ternary inside a template literal) and the literal segments of
 * template literals, with `${...}` holes skipped.
 */
function classRuns(source: string): string[] {
  const runs: string[] = [];
  for (const match of source.matchAll(/(['"])((?:[^'"\\\n]|\\.)*?)\1/g)) runs.push(match[2]);
  for (const match of source.matchAll(/`((?:[^`\\]|\\.)*)`/gs)) {
    const body = match[1];
    let index = 0;
    while (index < body.length) {
      const hole = body.indexOf('${', index);
      if (hole === -1) {
        runs.push(body.slice(index));
        break;
      }
      runs.push(body.slice(index, hole));
      let depth = 1;
      let cursor = hole + 2;
      while (cursor < body.length && depth > 0) {
        if (body[cursor] === '{') depth += 1;
        else if (body[cursor] === '}') depth -= 1;
        cursor += 1;
      }
      index = cursor;
    }
  }
  return runs;
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((entry) => {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) return sourceFiles(full);
    if (!/\.tsx?$/.test(entry) || /\.test\.tsx?$/.test(entry)) return [];
    return [full];
  });
}

function gapsIn(file: string): string[] {
  const source = readFileSync(file, 'utf8');
  const gaps: string[] = [];
  for (const run of classRuns(source)) {
    const tokens = run.split(/\s+/).filter(Boolean);
    if (tokens.length === 0) continue;
    if (!tokens.some((token) => LIGHT_GROUND.test(token))) continue;
    if (INVERTED_SURFACE.test(run)) continue;
    const covered = new Set(
      tokens.filter((t) => t.startsWith('dark:')).map((t) => propertyOf(t.slice(5))),
    );
    for (const token of tokens) {
      if (token.startsWith('dark:') || isNeutral(token)) continue;
      if (!LIGHT_GROUND.test(token)) continue;
      if (covered.has(propertyOf(token))) continue;
      gaps.push(`${relative(SRC, file)}: "${token}" in \`${run.trim().slice(0, 90)}\``);
    }
  }
  return gaps;
}

describe('dark mode coverage', () => {
  it('gives every light-ground utility a dark counterpart', () => {
    const gaps = sourceFiles(SRC).flatMap(gapsIn);
    expect(gaps).toEqual([]);
  });

  it('detects a missing counterpart', () => {
    // Guards the guard: a checker that silently matches nothing would pass the
    // assertion above for the wrong reason.
    const runs = classRuns('const a = "rounded-lg bg-white p-2";');
    expect(runs.some((run) => run.includes('bg-white'))).toBe(true);
    const tokens = 'rounded-lg bg-white p-2'.split(' ');
    expect(tokens.filter((t) => LIGHT_GROUND.test(t))).toEqual(['bg-white']);
  });
});
