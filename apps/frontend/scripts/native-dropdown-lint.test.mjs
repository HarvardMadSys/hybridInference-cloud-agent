import { fileURLToPath } from 'node:url';
import { ESLint } from 'eslint';
import { describe, expect, it } from 'vitest';

// Lint snippets with the project's own configuration, as `npm run lint` does,
// so the guard is proven against the rules contributors actually run.
const eslint = new ESLint({ cwd: fileURLToPath(new URL('..', import.meta.url)) });

async function lint(code) {
  const [result] = await eslint.lintText(code, { filePath: 'src/components/Example.tsx' });
  return result.messages;
}

describe('native dropdown lint guard', () => {
  it('rejects a native select and names the shared Select', async () => {
    const messages = await lint(
      [
        'export function Example() {',
        '  return (',
        '    <select>',
        '      <option value="a">A</option>',
        '    </select>',
        '  );',
        '}',
        '',
      ].join('\n'),
    );

    expect(messages).toEqual([
      expect.objectContaining({
        ruleId: 'no-restricted-syntax',
        severity: 2,
        line: 3,
        message:
          'Use the shared themed Select from @/components/ui/Select instead of a native <select>.',
      }),
    ]);
  });

  it('rejects a native datalist and names SuggestionInput', async () => {
    const messages = await lint(
      [
        'export function Example() {',
        '  return (',
        '    <>',
        '      <input list="models" />',
        '      <datalist id="models">',
        '        <option value="a" />',
        '      </datalist>',
        '    </>',
        '  );',
        '}',
        '',
      ].join('\n'),
    );

    expect(messages).toEqual([
      expect.objectContaining({
        ruleId: 'no-restricted-syntax',
        severity: 2,
        line: 5,
        message:
          'Use SuggestionInput from @/components/ui/SuggestionInput instead of a native <datalist>.',
      }),
    ]);
  });

  it('accepts the shared Select and SuggestionInput', async () => {
    const messages = await lint(
      [
        "import { Select } from '@/components/ui/Select';",
        "import { SuggestionInput } from '@/components/ui/SuggestionInput';",
        '',
        'export function Example() {',
        '  return (',
        '    <>',
        '      <Select',
        '        value="a"',
        '        onValueChange={() => undefined}',
        "        options={[{ value: 'a', label: 'A' }]}",
        '      />',
        "      <SuggestionInput suggestions={['a']} />",
        '    </>',
        '  );',
        '}',
        '',
      ].join('\n'),
    );

    expect(messages).toEqual([]);
  });
});
