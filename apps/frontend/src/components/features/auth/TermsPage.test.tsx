// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import type { AuthOptions } from '@/lib/api/accounts';
import { TermsPage } from './TermsPage';

const options = vi.hoisted(() => ({ current: null as Partial<AuthOptions> | null }));
vi.mock('./useAuthOptions', () => ({ useAuthOptions: () => options.current }));

beforeEach(() => localStorage.clear());
afterEach(cleanup);

it("shows the deployment's Terms file in the reader's language", async () => {
  options.current = {
    terms_url: '',
    terms_text: 'English terms.',
    terms_by_locale: { en: 'English terms.', 'zh-CN': '中文条款。\n\n第二段。' },
  };
  localStorage.setItem('agents.locale', 'zh-CN');
  render(
    <LocaleProvider>
      <TermsPage />
    </LocaleProvider>,
  );
  expect(await screen.findByText('中文条款。')).toBeInTheDocument();
  expect(screen.getByText('第二段。')).toBeInTheDocument();
  expect(screen.queryByText('English terms.')).toBeNull();
});

it('falls back to the Admin text when the deployment has no Terms files', () => {
  options.current = { terms_url: '', terms_text: 'Admin terms.', terms_by_locale: {} };
  render(<TermsPage />);
  expect(screen.getByText('Admin terms.')).toBeInTheDocument();
});
