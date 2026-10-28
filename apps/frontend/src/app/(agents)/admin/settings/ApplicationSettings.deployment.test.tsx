// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { getAgentAdminSettings } from '@/lib/api/adminSettings';
import ApplicationSettings from './ApplicationSettings';
import { applicationSnapshot as snapshot } from './settingsTestFixtures';

vi.mock('@/lib/api/adminSettings', () => ({
  getAgentAdminSettings: vi.fn(),
  setAgentAdminSettings: vi.fn(),
}));
const read = vi.mocked(getAgentAdminSettings);
afterEach(cleanup);

describe('deployment guidance during recovery setup', () => {
  it('keeps recovery cross-page actions as text and only reveals same-page fields', async () => {
    read.mockResolvedValue(
      snapshot({
        definitions: [
          ...snapshot().definitions,
          {
            key: 'pgadmin_url',
            label: 'Private pgAdmin URL',
            description: 'Where pgAdmin is reached.',
            category: 'Database tools',
            kind: 'string',
            default: '',
            secret: false,
          },
        ],
        bootstrap: [
          {
            id: 'database',
            key: 'AGENT_DATABASE_URL',
            label: 'Database connection',
            description: 'Deployment-managed connection.',
            actions: [{ target: 'database', label: 'Open Admin Database' }],
          },
          {
            id: 'pgadmin_bootstrap',
            key: 'AGENT_PGADMIN_BOOTSTRAP_URL',
            label: 'pgAdmin first-time setup',
            description: 'Deployment routing.',
            actions: [{ target: 'pgadmin_url', label: 'Edit pgAdmin URL' }],
          },
        ],
      }),
    );
    render(<ApplicationSettings recoveryToken="test-recovery-token" />);
    const edit = await screen.findByRole('button', { name: 'Edit pgAdmin URL' });
    expect(screen.queryByRole('link', { name: 'Open Admin Database' })).not.toBeInTheDocument();
    expect(screen.getByText(/Open Admin Database — Gateway sign-in is required/)).toBeVisible();
    fireEvent.click(edit);
    expect(screen.getByRole('region', { name: 'Operations' })).toBeVisible();
    expect(window.location.href).not.toContain('test-recovery-token');
    expect(document.body).not.toHaveTextContent('test-recovery-token');
  });
});
