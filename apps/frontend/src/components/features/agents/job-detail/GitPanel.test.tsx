// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { AgentDiffFile } from '../types';
import { GitPanel } from './GitPanel';
import { chooseOption } from '@/components/ui/__tests__/select';

function diff(path: string, length: number): AgentDiffFile {
  return {
    path,
    add: length,
    del: 0,
    lines: Array.from({ length }, (_, index) => ({ marker: 'add', text: `+line ${index}` })),
  };
}

const LOCKFILE = diff('package-lock.json', 8001);
const YARN_LOCK = diff('yarn.lock', 4002);

function job(diffFileDetails: AgentDiffFile[]) {
  return {
    id: 'job',
    repo: 'owner/repository',
    baseSha: 'abcdef1',
    branch: 'agent/task',
    diffFileDetails,
    // The turn's recorded diff: there is no live workspace to read.
    workspaceGitAvailable: false,
  };
}

function diffRegion(path: string) {
  return screen.getByRole('region', { name: `Diff for ${path}` });
}

function drawnRows(path: string) {
  return diffRegion(path).querySelectorAll('code > span');
}

// Counts are written as the panel writes them, in whatever locale the run has.
const count = (value: number) => value.toLocaleString();
const note = (shown: number, total: number) =>
  `Diff limited to the first ${count(shown)} of ${count(total)} lines.`;
const MORE = `Show ${count(4000)} more lines`;

afterEach(() => cleanup());

describe('Git panel diff length', () => {
  it('draws the first 4,000 lines of a long diff and says how many it shows', () => {
    render(<GitPanel job={job([LOCKFILE])} active />);

    const rows = drawnRows('package-lock.json');
    expect(rows).toHaveLength(4000);
    expect(rows[3999]).toHaveTextContent('+line 3999');
    expect(screen.getByText(note(4000, 8001))).toBeInTheDocument();
    expect(screen.getByRole('button', { name: MORE })).toHaveAttribute('type', 'button');
  });

  it('draws 4,000 more lines on each request, then the rest, and offers no more', () => {
    render(<GitPanel job={job([LOCKFILE])} active />);

    fireEvent.click(screen.getByRole('button', { name: MORE }));
    expect(drawnRows('package-lock.json')).toHaveLength(8000);
    expect(screen.getByText(note(8000, 8001))).toBeInTheDocument();

    const rest = screen.getByRole('button', { name: `Show the remaining ${count(1)} line` });
    rest.focus();
    fireEvent.click(rest);
    const rows = drawnRows('package-lock.json');
    expect(rows).toHaveLength(8001);
    expect(rows[8000]).toHaveTextContent('+line 8000');
    expect(screen.queryByText(/Diff limited/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^Show / })).not.toBeInTheDocument();
    // The button pressed is gone; the keyboard stays in the diff it drew.
    expect(diffRegion('package-lock.json')).toHaveFocus();
  });

  it('starts again from the first 4,000 lines when another file is chosen', () => {
    render(<GitPanel job={job([LOCKFILE, YARN_LOCK])} active />);
    fireEvent.click(screen.getByRole('button', { name: MORE }));
    expect(drawnRows('package-lock.json')).toHaveLength(8000);

    const picker = screen.getByRole('combobox', { name: 'Changed file' });
    chooseOption(picker, /^yarn\.lock /);
    expect(drawnRows('yarn.lock')).toHaveLength(4000);
    expect(screen.getByText(note(4000, 4002))).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: `Show the remaining ${count(2)} lines` }),
    ).toBeInTheDocument();

    chooseOption(picker, /^package-lock\.json /);
    expect(drawnRows('package-lock.json')).toHaveLength(4000);
    expect(screen.getByText(note(4000, 8001))).toBeInTheDocument();
  });

  it('keeps the lines asked for while the diff is unchanged, and starts again for a new one', () => {
    const files = [LOCKFILE];
    const view = render(<GitPanel job={job(files)} active />);
    fireEvent.click(screen.getByRole('button', { name: MORE }));

    // Drawn again around the same diff, as a streamed event redraws the page.
    view.rerender(<GitPanel job={job(files)} active />);
    expect(drawnRows('package-lock.json')).toHaveLength(8000);

    view.rerender(<GitPanel job={job([diff('package-lock.json', 9000)])} active />);
    expect(drawnRows('package-lock.json')).toHaveLength(4000);
    expect(screen.getByText(note(4000, 9000))).toBeInTheDocument();
  });
});
