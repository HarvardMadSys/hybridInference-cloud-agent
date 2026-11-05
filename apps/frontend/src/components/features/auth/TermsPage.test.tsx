// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { LocaleProvider } from '@/components/providers/LocaleProvider';
import type { AuthOptions } from '@/lib/api/accounts';
import { TermsPage } from './TermsPage';

const state = vi.hoisted(() => ({
  options: null as Partial<AuthOptions> | null,
  loading: false,
}));
vi.mock('./useAuthOptions', () => ({
  useAuthOptionsState: () => ({ options: state.options, loading: state.loading }),
}));

const STRUCTURED = [
  'Last updated: November 4, 2026.',
  '1. About these Terms. Example Service is run by the example operator. By using it you agree.',
  '2. Accounts. Keep your password secure.\nYou are responsible for your account.',
  'A paragraph that continues section two.',
].join('\n\n');

function renderPage(locale?: string) {
  if (locale) localStorage.setItem('agents.locale', locale);
  return render(
    <LocaleProvider>
      <TermsPage />
    </LocaleProvider>,
  );
}

/** The text a reader sees, whitespace aside: nothing dropped, nothing added. */
function words(text: string) {
  return text.replace(/\s+/g, '');
}

beforeEach(() => {
  localStorage.clear();
  state.loading = false;
});
afterEach(cleanup);

it("shows the deployment's Terms file in the reader's language", async () => {
  state.options = {
    terms_url: '',
    terms_text: 'English terms.',
    terms_by_locale: { en: 'English terms.', 'zh-CN': '中文条款。\n\n第二段。' },
  };
  renderPage('zh-CN');
  expect(await screen.findByText('中文条款。')).toBeInTheDocument();
  expect(screen.getByText('第二段。')).toBeInTheDocument();
  expect(screen.queryByText('English terms.')).toBeNull();
  expect(screen.queryByText(/未提供你所用语言的版本/)).toBeNull();
});

it('falls back to the Admin text when the deployment has no Terms files', () => {
  state.options = { terms_url: '', terms_text: 'Admin terms.', terms_by_locale: {} };
  render(<TermsPage />);
  expect(screen.getByText('Admin terms.')).toBeInTheDocument();
});

it('sets the date under the title and gives each numbered section a heading', () => {
  state.options = { terms_url: '', terms_text: STRUCTURED };
  render(<TermsPage />);
  const title = screen.getByRole('heading', { level: 2, name: 'Terms of Service' });
  expect(title.parentElement).toHaveTextContent('Last updated: November 4, 2026.');
  const sections = screen.getAllByRole('heading', { level: 3 });
  expect(sections.map((heading) => heading.textContent)).toEqual([
    '1.About these Terms.',
    '2.Accounts.',
  ]);
  expect(
    screen.getByText('Example Service is run by the example operator. By using it you agree.'),
  ).toBeInTheDocument();
  expect(screen.getByText('A paragraph that continues section two.')).toBeInTheDocument();
  expect(words(screen.getByRole('article').textContent ?? '')).toContain(words(STRUCTURED));
});

it('keeps unstructured text whole, and shows markup as text rather than running it', () => {
  const text =
    'Plain terms without numbers.\n\n<b>Bold?</b> <img src=x onerror="alert(1)">\n\n1.5 GB is a size, not a section.';
  state.options = { terms_url: '', terms_text: text };
  const { container } = render(<TermsPage />);
  expect(screen.queryAllByRole('heading', { level: 3 })).toEqual([]);
  expect(screen.getByText('<b>Bold?</b> <img src=x onerror="alert(1)">')).toBeInTheDocument();
  expect(container.querySelector('article img, article b')).toBeNull();
  expect(words(screen.getByRole('article').textContent ?? '')).toContain(words(text));
});

it('says which language it shows when the reader’s was not published', () => {
  state.options = { terms_url: '', terms_text: '', terms_by_locale: { 'zh-CN': '中文条款。' } };
  renderPage('en');
  expect(screen.getByText('中文条款。').closest('[lang]')).toHaveAttribute('lang', 'zh-Hans');
  expect(screen.getByText('Not published in your language. Shown in 简体中文.')).toBeVisible();
});

it('prefers text it can show over a link, and offers the link as well', () => {
  state.options = { terms_url: 'https://example.test/terms', terms_text: 'Shown here.' };
  render(<TermsPage />);
  expect(screen.getByText('Shown here.')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: /Also published on another site/ })).toHaveAttribute(
    'href',
    'https://example.test/terms',
  );
});

it('links to Terms published only elsewhere, and links nothing that is not http(s)', () => {
  state.options = { terms_url: 'https://example.test/terms', terms_text: '' };
  const { unmount } = render(<TermsPage />);
  expect(screen.getByRole('link', { name: /Open the Terms of Service/ })).toHaveAttribute(
    'href',
    'https://example.test/terms',
  );
  unmount();
  state.options = { terms_url: 'javascript:alert(1)', terms_text: '' };
  render(<TermsPage />);
  expect(screen.queryByRole('link', { name: /Terms of Service/ })).toBeNull();
  expect(screen.getByText(/has not published its terms here/)).toBeInTheDocument();
});

it('waits while loading, and says so when the Terms could not be read', () => {
  state.options = null;
  state.loading = true;
  const { rerender } = render(<TermsPage />);
  expect(screen.getByRole('status')).toHaveTextContent('Loading…');
  state.loading = false;
  rerender(<TermsPage />);
  expect(screen.getByRole('alert')).toHaveTextContent('The Terms of Service could not be loaded.');
  expect(screen.getByRole('button', { name: 'Retry' })).toBeInTheDocument();
});

it('treats whitespace-only text as nothing published', () => {
  state.options = { terms_url: '', terms_text: '  \n\n ', terms_by_locale: { en: ' ' } };
  render(<TermsPage />);
  expect(screen.getByText(/has not published its terms here/)).toBeInTheDocument();
});
