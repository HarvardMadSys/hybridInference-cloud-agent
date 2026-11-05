// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render } from '@testing-library/react';
import type { ReactElement } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { LOCALE_STORAGE_KEY } from '@/lib/i18n/locales';

import GlobalError from './global-error';

// It replaces the root layout, `<html>` included, so it is mounted on the
// document rather than inside a `<div>`, and queried from there: `screen` is
// bound to the `<body>` it replaced.
let unmount: (() => void) | null = null;

function renderDocument(ui: ReactElement) {
  const view = render(ui, { container: document });
  unmount = view.unmount;
  return view;
}

afterEach(() => {
  // Unmounting takes that `<html>` away, and RTL's cleanup reads
  // `document.body`, so the document gets an empty one back first.
  unmount?.();
  unmount = null;
  document.append(document.createElement('html'));
  document.documentElement.append(document.createElement('body'));
  cleanup();
  localStorage.clear();
});

describe('root layout error boundary', () => {
  it('renders a document of its own, runs no script, and retries the layout', () => {
    const reset = vi.fn();
    const { getByRole, queryByText } = renderDocument(
      <GlobalError error={new Error('AGENT_WEB_STATUS_URL is not a URL')} reset={reset} />,
    );

    expect(getByRole('heading', { name: 'This page could not be displayed' })).toBeVisible();
    expect(queryByText('AGENT_WEB_STATUS_URL is not a URL')).not.toBeInTheDocument();
    expect(document.querySelector('script')).toBeNull();
    fireEvent.click(getByRole('button', { name: 'Retry' }));
    expect(reset).toHaveBeenCalledOnce();
  });

  it("keeps the reader's language without the layout's providers", () => {
    localStorage.setItem(LOCALE_STORAGE_KEY, 'zh-CN');
    const { getByRole } = renderDocument(
      <GlobalError error={new Error('boom')} reset={() => {}} />,
    );

    expect(getByRole('heading', { name: '无法显示此页面' })).toBeVisible();
    expect(document.documentElement).toHaveAttribute('lang', 'zh-Hans');
  });
});
