// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import AgentsError from './error';

afterEach(cleanup);

describe('agent page error boundary', () => {
  it('stands in for the page that failed and retries it', () => {
    const reset = vi.fn();
    render(<AgentsError error={new Error('entries is not iterable')} reset={reset} />);

    expect(screen.getByRole('heading', { name: 'This page could not be displayed' })).toBeVisible();
    expect(screen.queryByText('entries is not iterable')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    expect(reset).toHaveBeenCalledOnce();
  });
});
