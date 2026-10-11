import { describe, expect, it } from 'vitest';
import { toolNeedsAttention, toolSummary } from './toolSummary';
import type { AgentEvent } from './types';

type ToolUseEvent = Extract<AgentEvent, { kind: 'tool_use' }>;

function tool(tool: string, input?: unknown, extra: Partial<ToolUseEvent> = {}): ToolUseEvent {
  return { kind: 'tool_use', tool, detail: '', input, ...extra };
}

describe('recorded tool summaries', () => {
  it('shows commands and descriptions without deriving an outcome', () => {
    expect(toolSummary(tool('Bash', { command: 'npm test', description: 'Run tests' }))).toBe(
      'Bash: npm test',
    );
    expect(
      toolSummary(tool('functions.exec_command', JSON.stringify({ cmd: 'rg tool src' }))),
    ).toBe('functions.exec_command: rg tool src');
    expect(toolSummary(tool('shell', { description: 'Inspect the repository' }))).toBe(
      'shell: Inspect the repository',
    );
    expect(toolSummary(tool('Bash', { command: 'false' }, { resultState: 'failed' }))).toBe(
      'Bash: false',
    );
  });

  it.each(['Read', 'Edit', 'Write'])('shows the recorded file path for %s', (name) => {
    expect(toolSummary(tool(name, { file_path: 'src/app.ts', content: 'A long file body' }))).toBe(
      `${name}: src/app.ts`,
    );
    expect(toolSummary(tool(`functions.${name.toLowerCase()}_file`, { path: 'README.md' }))).toBe(
      `functions.${name.toLowerCase()}_file: README.md`,
    );
  });

  it('includes search scope only when it was recorded', () => {
    expect(toolSummary(tool('Grep', { pattern: 'AgentEvent', path: 'src' }))).toBe(
      'Grep: AgentEvent in src',
    );
    expect(toolSummary(tool('Glob', { pattern: '**/*.tsx' }))).toBe('Glob: **/*.tsx');
    expect(toolSummary(tool('search', { query: 'composer', directory: 'apps/frontend' }))).toBe(
      'search: composer in apps/frontend',
    );
  });

  it('falls back to recorded detail or tool name for malformed and unknown arguments', () => {
    expect(toolSummary(tool('Read', '{broken', { detail: 'README.md' }))).toBe('Read: README.md');
    expect(
      toolSummary(tool('Mystery', { nested: { body: 'Do not dump JSON' } }, { detail: 'Inspect' })),
    ).toBe('Mystery: Inspect');
    expect(toolSummary(tool('Mystery', ['unexpected', 'array']))).toBe('Mystery');
    expect(toolSummary(tool('Mystery', { command: 'Do not guess tool semantics' }))).toBe(
      'Mystery',
    );
    expect(
      toolSummary(tool('Agent', { description: 'Review accessibility', prompt: 'Long prompt' })),
    ).toBe('Agent: Review accessibility');
  });

  it('keeps multiline and long recorded inputs compact', () => {
    expect(toolSummary(tool('Bash', { command: 'npm\n  test\t-- --run' }))).toBe(
      'Bash: npm test -- --run',
    );
    const summary = toolSummary(
      tool('Write', { path: `src/${'long-directory/'.repeat(30)}file.ts` }),
    );
    expect(summary.startsWith('Write: src/')).toBe(true);
    expect(summary.endsWith('…')).toBe(true);
    expect(summary.length).toBeLessThanOrEqual(180);
  });
});

describe('tool attention from recorded result evidence', () => {
  it.each(['failed', 'cancelled', 'unknown', 'not_recorded', 'waiting'] as const)(
    'preserves %s calls even when an output preview exists',
    (resultState) => {
      expect(toolNeedsAttention(tool('Read', {}, { resultState, output: ['A preview'] }))).toBe(
        true,
      );
    },
  );

  it('preserves recorded errors and calls missing result evidence', () => {
    expect(
      toolNeedsAttention(tool('Bash', {}, { resultState: 'received', outputIsError: true })),
    ).toBe(true);
    expect(toolNeedsAttention(tool('Bash', { command: 'echo success' }))).toBe(true);
  });

  it('accepts received states and recorded results including an empty result', () => {
    expect(toolNeedsAttention(tool('Read', {}, { resultState: 'received' }))).toBe(false);
    expect(toolNeedsAttention(tool('Read', {}, { output: ['file content'] }))).toBe(false);
    expect(toolNeedsAttention(tool('Read', {}, { output: [] }))).toBe(false);
  });
});
