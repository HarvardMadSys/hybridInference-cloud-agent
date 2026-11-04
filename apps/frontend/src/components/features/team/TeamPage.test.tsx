// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';

import { TeamPage } from './TeamPage';

afterEach(cleanup);

it('lists each member with their role and website', () => {
  render(<TeamPage />);
  expect(screen.getByRole('heading', { name: 'Team' })).toBeInTheDocument();
  const juncheng = screen.getByRole('heading', { name: 'Juncheng Yang' }).closest('li')!;
  expect(juncheng).toHaveTextContent('Lead');
  expect(juncheng).toHaveTextContent('Assistant Professor at Harvard University');
  const murphy = screen.getByRole('heading', { name: 'Murphy Tian' }).closest('li')!;
  expect(murphy).toHaveTextContent('Core developer');
  expect(within(murphy).getByRole('link', { name: 'Website' })).toHaveAttribute(
    'href',
    'https://realtmxi.github.io/',
  );
  expect(screen.getAllByRole('heading', { level: 3 })).toHaveLength(2);
});
