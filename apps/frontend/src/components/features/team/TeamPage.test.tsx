// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';

import { SiteConfigProvider } from '@/components/providers/SiteConfigProvider';
import { parseSavedSiteConfig } from '@/config/site';
import { TeamPage } from './TeamPage';

afterEach(cleanup);

const saved = {
  branding: { appName: 'Acme Agent', orgName: '', orgUrl: '', statusUrl: '' },
  content: {
    sourceUrl: '',
    team: [
      {
        name: 'Ada Lovelace',
        photoUrl: 'https://ada.example.org/portrait.jpg',
        website: 'https://ada.example.org/',
        badge: { en: 'Lead', 'zh-CN': '负责人' },
        roles: [{ en: 'Professor', 'zh-CN': '教授' }, { 'zh-CN': '仅中文' }],
      },
      { name: 'Grace Hopper', website: 'javascript:alert(1)', badge: {}, roles: [] },
      { website: 'https://nameless.example/' },
    ],
  },
};

it('lists the members the deployment declared, in its words', () => {
  render(
    <SiteConfigProvider config={parseSavedSiteConfig(saved)}>
      <TeamPage />
    </SiteConfigProvider>,
  );
  expect(
    screen.getByRole('heading', { name: 'The people behind Acme Agent.', level: 2 }),
  ).toBeInTheDocument();
  const ada = screen.getByRole('heading', { name: 'Ada Lovelace' }).closest('li')!;
  expect(ada).toHaveTextContent('Lead');
  expect(ada).toHaveTextContent('Professor');
  // A label written only in another language still shows.
  expect(ada).toHaveTextContent('仅中文');
  expect(within(ada).getByRole('img', { name: 'Photo of Ada Lovelace' })).toHaveAttribute(
    'src',
    'https://ada.example.org/portrait.jpg',
  );
  expect(within(ada).getByRole('link', { name: 'Website' })).toHaveAttribute(
    'href',
    'https://ada.example.org/',
  );
  const grace = screen.getByRole('heading', { name: 'Grace Hopper' }).closest('li')!;
  // Not an http(s) URL, so no link.
  expect(within(grace).queryByRole('link')).toBeNull();
  // A member without a name is dropped.
  expect(screen.getAllByRole('heading', { level: 3 })).toHaveLength(2);
});

it('falls back on a failed photo and tries a newly configured URL', () => {
  const page = (photoUrl: string) => (
    <SiteConfigProvider
      config={parseSavedSiteConfig({
        ...saved,
        content: { team: [{ ...saved.content.team[0], photoUrl }] },
      })}
    >
      <TeamPage />
    </SiteConfigProvider>
  );
  const { rerender } = render(page('https://ada.example.org/broken.jpg'));
  fireEvent.error(screen.getByRole('img', { name: 'Photo of Ada Lovelace' }));
  expect(screen.queryByRole('img')).toBeNull();
  expect(screen.getByText('AL')).toBeInTheDocument();
  rerender(page('https://ada.example.org/replacement.jpg'));
  expect(screen.getByRole('img', { name: 'Photo of Ada Lovelace' })).toHaveAttribute(
    'src',
    'https://ada.example.org/replacement.jpg',
  );
});

it('says so when the deployment published no team', () => {
  render(<TeamPage />);
  expect(screen.getByText('This deployment has not published its team.')).toBeInTheDocument();
  expect(screen.queryByRole('heading', { level: 3 })).toBeNull();
});
