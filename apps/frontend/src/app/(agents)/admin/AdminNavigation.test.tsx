// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AdminNavigation } from './AdminNavigation';

describe('AdminNavigation', () => {
  it('lists Settings last, after the pages an administrator reads', () => {
    render(<AdminNavigation current="settings" />);
    const nav = screen.getByRole('navigation', { name: 'Admin sections' });
    const labels = within(nav)
      .getAllByRole('link')
      .map((link) => link.textContent);
    expect(labels).toEqual([
      'Users',
      'Servers',
      'Sandboxes',
      'Activity',
      'Usage',
      'Measurements',
      'Database',
      'Audit log',
      'Settings',
    ]);
    expect(within(nav).getByRole('link', { name: 'Settings' })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });
});
