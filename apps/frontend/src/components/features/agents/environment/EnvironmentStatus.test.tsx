// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { EnvironmentSteps } from './EnvironmentStatus';
vi.mock('@/components/providers', () => ({ useT: () => (key: string) => key }));
afterEach(cleanup);

it('shows an installation taken from a snapshot as such, then the update it ran', () => {
  render(
    <EnvironmentSteps
      steps={[
        {
          phase: 'setup',
          state: 'succeeded',
          output: '',
          ran: false,
          snapshot_id: 's',
          detail: 'Installed from a snapshot',
        },
        { phase: 'update', state: 'succeeded', output: 'generated', duration_seconds: 2 },
        { phase: 'start', state: 'succeeded', output: '' },
      ]}
    />,
  );
  expect(screen.getByText('environment.fromSnapshot')).toBeInTheDocument();
  expect(screen.getByText('environment.phase.update')).toBeInTheDocument();
  // The host's English detail is not shown as an error beside it.
  expect(screen.queryByText('Installed from a snapshot')).toBeNull();
});

it('shows an installation kept through a restore without calling it a snapshot', () => {
  render(
    <EnvironmentSteps steps={[{ phase: 'setup', state: 'succeeded', output: '', ran: false }]} />,
  );
  expect(screen.getByText('environment.keptAfterRestore')).toBeInTheDocument();
  expect(screen.queryByText('environment.fromSnapshot')).toBeNull();
});
