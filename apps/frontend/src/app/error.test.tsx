// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import RootError from './error';

afterEach(cleanup);

describe('root error boundary', () => {
  it('stands in for a page outside the agent shell and retries it', () => {
    const reset = vi.fn();
    render(<RootError error={new Error('params is not iterable')} reset={reset} />);

    expect(screen.getByRole('heading', { name: 'This page could not be displayed' })).toBeVisible();
    expect(screen.queryByText('params is not iterable')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(reset).toHaveBeenCalledOnce();
  });
});
