// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { Markdown } from './Markdown';

afterEach(cleanup);

describe('Markdown', () => {
  it('renders markdown as HTML rather than literal text', () => {
    render(
      <Markdown
        text={'Get a **key** from the [dashboard](https://x.test).\n\n1. one\n2. two\n3. three'}
      />,
    );

    // **key** -> <strong>, not literal asterisks
    expect(screen.getByText('key').tagName).toBe('STRONG');
    expect(screen.queryByText(/\*\*key\*\*/)).toBeNull();

    // [dashboard](url) -> anchor opening in a new tab
    const link = screen.getByRole('link', { name: 'dashboard' });
    expect(link).toHaveAttribute('href', 'https://x.test');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', expect.stringContaining('noopener'));

    // ordered list -> three <li>
    expect(screen.getAllByRole('listitem')).toHaveLength(3);
  });

  it('renders inline code and fenced code blocks', () => {
    const { container } = render(
      <Markdown text={'run `npm test` first\n\n```bash\necho hi\n```'} />,
    );
    expect(container.querySelector('code')).not.toBeNull();
    expect(container.querySelector('pre')).not.toBeNull();
    expect(screen.getByText('npm test').tagName).toBe('CODE');
  });

  it('styles an unlabelled fence as a block, not as inline code', () => {
    // A fence with no language has no `language-*` class, which is exactly the
    // case that used to fall through to the inline chip and render one chip
    // per line of an ASCII diagram.
    const { container } = render(
      <Markdown text={'see `README.md`\n\n```\nbrowser ──▶ web/\n   ▼\ncloud_agent/\n```'} />,
    );
    const inline = screen.getByText('README.md');
    const block = container.querySelector('pre > code');
    expect(block).not.toBeNull();
    expect(inline.className).toContain('bg-gray-950/[0.06]');
    expect(block!.className).not.toContain('bg-gray-950/[0.06]');
    expect(block!.textContent).toBe('browser ──▶ web/\n   ▼\ncloud_agent/');
  });

  it('colours the keywords of a fence that names its language', () => {
    const { container } = render(
      <Markdown text={'```python\ndef go(x):\n    return "ok"  # done\n```'} />,
    );
    const block = container.querySelector('pre > code')!;
    const spans = [...block.querySelectorAll('span')].map((span) => span.textContent);
    expect(spans).toEqual(expect.arrayContaining(['def', 'return', '"ok"', '# done']));
    // Colouring must not alter the text itself.
    expect(block.textContent).toBe('def go(x):\n    return "ok"  # done');
  });
});
