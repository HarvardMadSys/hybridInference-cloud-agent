// The script the home page's product demo plays.
//
// Everything here is data: three example tasks, and for each stage of a task a
// list of beats — wait this long, then change these fields of the window. The
// player only schedules beats; the window only draws a frame. Jumping to the
// end of a stage is the same beats folded without waiting, so a jump and a
// playback can never disagree about what the window shows.

export const AGENT_IDS = ['claude', 'codex', 'pi', 'opencode', 'kilo'] as const;
export type AgentId = (typeof AGENT_IDS)[number];

/** Display names, as the app's own model connection pages spell them. */
export const AGENT_NAMES: Record<AgentId, string> = {
  claude: 'Claude Code',
  codex: 'Codex',
  pi: 'pi',
  opencode: 'OpenCode',
  kilo: 'Kilo Code',
};

/** The model pill the composer shows next to each agent. */
export const AGENT_MODELS: Record<AgentId, string> = {
  claude: 'sonnet · My key',
  codex: 'gpt-5 · My key',
  pi: 'sonnet · My key',
  opencode: 'sonnet · My key',
  kilo: 'sonnet · My key',
};

export type DiffKind = 'context' | 'added' | 'removed';
export type ResultTone = 'ok' | 'bad' | 'plain';
export type TermTone = 'command' | 'ok' | 'bad' | 'dim';

export interface DemoStep {
  kind: 'read' | 'edit' | 'run';
  path: string;
  /** Lines added and removed, for an edit. */
  diff?: { added?: number; removed?: number };
  /** A short result, for a run. */
  result?: string;
  tone?: ResultTone;
  /** How long the step runs before its result shows. */
  ms: number;
  /** Terminal output: the first line prints when the step starts, the rest when it ends. */
  terminal?: { tone: TermTone; text: string }[];
}

export interface DemoFile {
  path: string;
  added: number;
  removed: number;
  lines: { kind: DiffKind; number: number; text: string }[];
}

export interface DemoExample {
  agent: AgentId;
  title: string;
  prompt: string;
  /** How long the agent "worked", in seconds. */
  seconds: number;
  steps: DemoStep[];
  /** The agent's answer; `code` spans are wrapped in backticks. */
  answer: string;
  tree: { name: string; depth: number; folder?: boolean }[];
  files: DemoFile[];
  pr: { number: number; added: number; removed: number; files: number };
}

export const EXAMPLES: DemoExample[] = [
  {
    agent: 'claude',
    title: 'Add CSV export tests',
    prompt: 'Add tests for the CSV export, including empty files.',
    seconds: 31,
    steps: [
      { kind: 'read', path: 'src/export/csv.ts', ms: 420 },
      { kind: 'edit', path: 'tests/export/csv.test.ts', diff: { added: 48 }, ms: 560 },
      {
        kind: 'run',
        path: 'npm test -- csv',
        result: '1 failed',
        tone: 'bad',
        ms: 950,
        terminal: [
          { tone: 'command', text: '$ npm test -- csv' },
          { tone: 'bad', text: '✗ keeps the header for no rows' },
          { tone: 'dim', text: "  expected 'id,name,email\\n', got ''" },
        ],
      },
      { kind: 'edit', path: 'src/export/csv.ts', diff: { added: 3, removed: 1 }, ms: 520 },
      {
        kind: 'run',
        path: 'npm test -- csv',
        result: '12 passed',
        tone: 'ok',
        ms: 950,
        terminal: [
          { tone: 'command', text: '$ npm test -- csv' },
          { tone: 'ok', text: '✓ 12 passed (1.4s)' },
        ],
      },
    ],
    answer:
      'An empty file dropped the header row. Fixed in `csv.ts`, with tests for empty, quoted and multi-line rows.',
    tree: [
      { name: 'src/export/', depth: 0, folder: true },
      { name: 'csv.ts', depth: 1 },
      { name: 'xlsx.ts', depth: 1 },
      { name: 'index.ts', depth: 1 },
      { name: 'tests/export/', depth: 0, folder: true },
      { name: 'csv.test.ts', depth: 1 },
      { name: 'package.json', depth: 0 },
    ],
    files: [
      {
        path: 'tests/export/csv.test.ts',
        added: 48,
        removed: 0,
        lines: [
          { kind: 'context', number: 12, text: "describe('toCsv', () => {" },
          { kind: 'added', number: 13, text: "+  it('keeps the header for no rows', () => {" },
          { kind: 'added', number: 14, text: '+    expect(toCsv([], cols))' },
          { kind: 'added', number: 15, text: "+      .toBe('id,name,email\\n');" },
          { kind: 'added', number: 16, text: '+  });' },
          { kind: 'added', number: 17, text: "+  it('quotes commas and newlines', () => {" },
        ],
      },
      {
        path: 'src/export/csv.ts',
        added: 3,
        removed: 1,
        lines: [
          { kind: 'removed', number: 8, text: "-  if (!rows.length) return '';" },
          { kind: 'added', number: 8, text: "+  const head = cols.join(',') + '\\n';" },
          { kind: 'added', number: 9, text: '+  if (!rows.length) return head;' },
        ],
      },
    ],
    pr: { number: 128, added: 51, removed: 1, files: 2 },
  },
  {
    agent: 'codex',
    title: 'Fix login redirect',
    prompt: 'After sign-in, send people back to the page they asked for. Reject outside URLs.',
    seconds: 44,
    steps: [
      { kind: 'read', path: 'src/auth/redirect.ts', ms: 420 },
      { kind: 'read', path: 'src/app/login/page.tsx', ms: 420 },
      { kind: 'edit', path: 'src/auth/redirect.ts', diff: { added: 22, removed: 3 }, ms: 560 },
      { kind: 'edit', path: 'tests/auth/redirect.test.ts', diff: { added: 40 }, ms: 520 },
      {
        kind: 'run',
        path: 'npm test -- redirect',
        result: '8 passed',
        tone: 'ok',
        ms: 1050,
        terminal: [
          { tone: 'command', text: '$ npm test -- redirect' },
          { tone: 'ok', text: '✓ returns to the requested page' },
          { tone: 'ok', text: '✓ rejects https://other.example' },
          { tone: 'ok', text: '✓ 8 passed (0.9s)' },
        ],
      },
    ],
    answer:
      'Sign-in now keeps the page you asked for. Return URLs that point off the site fall back to `/`.',
    tree: [
      { name: 'src/auth/', depth: 0, folder: true },
      { name: 'redirect.ts', depth: 1 },
      { name: 'session.ts', depth: 1 },
      { name: 'src/app/login/', depth: 0, folder: true },
      { name: 'page.tsx', depth: 1 },
      { name: 'tests/auth/', depth: 0, folder: true },
      { name: 'redirect.test.ts', depth: 1 },
    ],
    files: [
      {
        path: 'src/auth/redirect.ts',
        added: 22,
        removed: 3,
        lines: [
          { kind: 'context', number: 19, text: 'export function finishSignIn(returnTo) {' },
          { kind: 'context', number: 20, text: '  setSession(user);' },
          { kind: 'removed', number: 21, text: "-  redirect('/');" },
          { kind: 'added', number: 21, text: '+  const target = safeReturnPath(returnTo);' },
          { kind: 'added', number: 22, text: '+  redirect(target);' },
        ],
      },
      {
        path: 'tests/auth/redirect.test.ts',
        added: 40,
        removed: 0,
        lines: [
          { kind: 'added', number: 5, text: "+it('rejects outside return URLs', () => {" },
          { kind: 'added', number: 6, text: "+  expect(safeReturnPath('https://other.example'))" },
          { kind: 'added', number: 7, text: "+    .toBe('/');" },
          { kind: 'added', number: 8, text: '+});' },
        ],
      },
    ],
    pr: { number: 131, added: 62, removed: 3, files: 2 },
  },
  {
    agent: 'opencode',
    title: 'Share one retry helper',
    prompt: 'Pull the duplicated retry logic into one helper. Keep the behavior the same.',
    seconds: 72,
    steps: [
      { kind: 'read', path: 'src/client/request.ts', ms: 420 },
      { kind: 'read', path: 'src/client/upload.ts', ms: 420 },
      { kind: 'edit', path: 'src/client/retry.ts', diff: { added: 31 }, ms: 560 },
      { kind: 'edit', path: '3 callers', diff: { added: 12, removed: 46 }, ms: 560 },
      {
        kind: 'run',
        path: 'npm test -- retry',
        result: '16 passed',
        tone: 'ok',
        ms: 1050,
        terminal: [
          { tone: 'command', text: '$ npm test -- retry' },
          { tone: 'ok', text: '✓ recovers after two failures' },
          { tone: 'ok', text: '✓ gives up after 3 attempts' },
          { tone: 'ok', text: '✓ 16 passed (1.1s)' },
        ],
      },
    ],
    answer:
      'Three callers now share `withRetry`. Tests cover success, recovery and running out of attempts.',
    tree: [
      { name: 'src/client/', depth: 0, folder: true },
      { name: 'request.ts', depth: 1 },
      { name: 'upload.ts', depth: 1 },
      { name: 'download.ts', depth: 1 },
      { name: 'tests/client/', depth: 0, folder: true },
      { name: 'retry.test.ts', depth: 1 },
      { name: 'package.json', depth: 0 },
    ],
    files: [
      {
        path: 'src/client/request.ts',
        added: 4,
        removed: 16,
        lines: [
          { kind: 'removed', number: 14, text: '-  for (let i = 0; i < attempts; i++) {' },
          { kind: 'removed', number: 15, text: '-    try { return await send(req); }' },
          { kind: 'added', number: 14, text: '+  return withRetry(() => send(req), {' },
          { kind: 'added', number: 15, text: '+    attempts, delay: retryDelay,' },
          { kind: 'added', number: 16, text: '+  });' },
        ],
      },
      {
        path: 'src/client/retry.ts',
        added: 31,
        removed: 0,
        lines: [
          { kind: 'added', number: 1, text: '+export async function withRetry(run, opts) {' },
          { kind: 'added', number: 2, text: '+  for (let i = 1; ; i++) {' },
          { kind: 'added', number: 3, text: '+    try { return await run(); }' },
        ],
      },
    ],
    pr: { number: 134, added: 43, removed: 46, files: 4 },
  },
];

export type DemoStage = 0 | 1 | 2;
export const STAGES: readonly DemoStage[] = [0, 1, 2];

/** Everything the product window draws, at one moment. */
export interface DemoFrame {
  agent: AgentId;
  /** The agent picker's highlighted row. */
  highlight: AgentId;
  picker: boolean;
  focus: boolean;
  typed: string;
  sendPressed: boolean;
  /** The task has been sent: the conversation and titles show it. */
  sent: boolean;
  worked: 'hidden' | 'running' | 'done';
  stepsStarted: number;
  stepsDone: number;
  /** Seconds the run clock shows. */
  clock: number;
  pane: 'files' | 'terminal' | 'changes';
  answer: boolean;
  row: 'hidden' | 'shown' | 'pressed' | 'busy' | 'opened';
  status: 'none' | 'running' | 'ready' | 'pr';
  checksPassed: boolean;
  dot: 'none' | 'running' | 'review';
}

export interface Beat {
  /** Milliseconds to wait before applying `patch`. */
  wait: number;
  patch: Partial<DemoFrame>;
}

/** The first frame of a task: a new conversation, on the agent last used. */
export function initialFrame(agent: AgentId): DemoFrame {
  return {
    agent,
    highlight: agent,
    picker: false,
    focus: false,
    typed: '',
    sendPressed: false,
    sent: false,
    worked: 'hidden',
    stepsStarted: 0,
    stepsDone: 0,
    clock: 0,
    pane: 'files',
    answer: false,
    row: 'hidden',
    status: 'none',
    checksPassed: false,
    dot: 'none',
  };
}

/** A typing rhythm: a pause after punctuation, a beat after each word. */
function keystrokeWait(previous: string | undefined, index: number): number {
  if (index === 0) return 240;
  if (previous === ',' || previous === '.') return 150;
  if (previous === ' ') return 36;
  return 25;
}

function giveTask(example: DemoExample): Beat[] {
  const beats: Beat[] = [
    { wait: 320, patch: { picker: true } },
    { wait: 480, patch: { highlight: example.agent } },
    { wait: 420, patch: { agent: example.agent } },
    { wait: 520, patch: { picker: false } },
    { wait: 300, patch: { focus: true } },
  ];
  for (let index = 0; index < example.prompt.length; index += 1) {
    beats.push({
      wait: keystrokeWait(example.prompt[index - 1], index),
      patch: { typed: example.prompt.slice(0, index + 1) },
    });
  }
  beats.push({ wait: 440, patch: { sendPressed: true } });
  return beats;
}

function followWork(example: DemoExample): Beat[] {
  const beats: Beat[] = [
    {
      wait: 180,
      patch: { sendPressed: false, focus: false, typed: '', sent: true, dot: 'running' },
    },
    { wait: 440, patch: { worked: 'running', status: 'running', pane: 'terminal' } },
  ];
  const count = example.steps.length;
  example.steps.forEach((step, index) => {
    beats.push({ wait: 240, patch: { stepsStarted: index + 1 } });
    beats.push({
      wait: step.ms,
      patch: {
        stepsDone: index + 1,
        clock: Math.round((example.seconds * (index + 1)) / (count + 1)),
      },
    });
  });
  beats.push({
    wait: 380,
    patch: { worked: 'done', status: 'ready', dot: 'review', clock: example.seconds },
  });
  return beats;
}

function reviewResult(): Beat[] {
  return [
    { wait: 300, patch: { pane: 'changes' } },
    { wait: 560, patch: { answer: true } },
    { wait: 420, patch: { row: 'shown' } },
    { wait: 1150, patch: { row: 'pressed' } },
    { wait: 170, patch: { row: 'busy' } },
    { wait: 900, patch: { row: 'opened', status: 'pr' } },
    { wait: 1400, patch: { checksPassed: true } },
  ];
}

/** The beats of one stage of an example. */
export function stageBeats(example: DemoExample, stage: DemoStage): Beat[] {
  if (stage === 0) return giveTask(example);
  if (stage === 1) return followWork(example);
  return reviewResult();
}

/** How long a stage plays, in milliseconds. */
export function stageDuration(example: DemoExample, stage: DemoStage): number {
  return stageBeats(example, stage).reduce((total, beat) => total + beat.wait, 0);
}

/**
 * The frame at the end of `stage` (or at the very start, for -1), reached by
 * applying every beat up to there.
 */
export function frameAfter(example: DemoExample, stage: DemoStage | -1, from: AgentId): DemoFrame {
  let frame = initialFrame(from);
  for (const each of STAGES) {
    if (each > stage) break;
    for (const beat of stageBeats(example, each)) frame = { ...frame, ...beat.patch };
  }
  return frame;
}

/** Which terminal lines are on screen. */
export function visibleTerminal(example: DemoExample, frame: DemoFrame) {
  return example.steps.flatMap((step, index) => {
    const lines = step.terminal ?? [];
    if (index < frame.stepsDone) return lines;
    if (index < frame.stepsStarted) return lines.slice(0, 1);
    return [];
  });
}
