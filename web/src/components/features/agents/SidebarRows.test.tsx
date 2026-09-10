// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { Highlight } from './SidebarRows';
afterEach(cleanup);
describe('literal search highlighting', () => {
  it('keeps original offsets after Unicode letters whose lowercase form changes length', () => {
    const { container } = render(<Highlight text="İstanbul deploy — DEPLOY" query="deploy" />);
    expect(container.textContent).toBe('İstanbul deploy — DEPLOY');
    expect([...container.querySelectorAll('mark')].map((mark) => mark.textContent)).toEqual([
      'deploy',
      'DEPLOY',
    ]);
  });
  it('treats metacharacters and HTML as literal text', () => {
    const text = 'a.*[x](y)$^?+|\\ <img src=x>';
    const { container, rerender } = render(<Highlight text={text} query={'.*[x](y)$^?+|\\'} />);
    expect(container.textContent).toBe(text);
    expect(container.querySelector('mark')?.textContent).toBe('.*[x](y)$^?+|\\');
    rerender(<Highlight text={text} query="<img src=x>" />);
    expect(container.querySelector('mark')?.textContent).toBe('<img src=x>');
    expect(container.querySelector('img')).toBeNull();
  });
  it('preserves surrogate pairs before and within a match', () => {
    const { container } = render(<Highlight text="🚀 build 🚀 deploy" query="🚀 deploy" />);
    expect(container.textContent).toBe('🚀 build 🚀 deploy');
    expect(container.querySelector('mark')?.textContent).toBe('🚀 deploy');
  });
});
