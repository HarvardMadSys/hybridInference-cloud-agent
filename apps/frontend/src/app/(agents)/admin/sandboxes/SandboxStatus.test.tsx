// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import type { AdminSandbox } from '@/lib/api/adminSandboxes';
import { SandboxStatus } from './SandboxStatus';

function sandbox(fields: Partial<AdminSandbox>): AdminSandbox {
  return { state: 'stopped', active_attempt_count: 0, ...fields } as AdminSandbox;
}

afterEach(cleanup);

describe('SandboxStatus', () => {
  it('shows a finished deletion in place of the lost state it leaves', () => {
    const { container } = render(
      <SandboxStatus
        sandbox={sandbox({ state: 'lost', deletion_state: 'deleted', deletion_discard: true })}
      />,
    );
    expect(container).toHaveTextContent(/^deleted$/);
  });

  it.each<[Partial<AdminSandbox>, string]>([
    [{ deletion_state: 'pending', deletion_discard: true }, 'Deletion pending'],
    [{ deletion_state: 'archived' }, 'Deletion pending'],
    [{ deletion_state: 'failed' }, 'Deletion failed'],
    [
      { deletion_state: 'archived', deletion_retry_exhausted_at: '2026-11-11T10:00:00Z' },
      'Deletion failed',
    ],
  ])('keeps the container state of %j and adds “%s”', (fields, note) => {
    render(<SandboxStatus sandbox={sandbox(fields)} />);
    expect(screen.getByText('stopped')).toBeInTheDocument();
    expect(screen.getByText(note)).toBeInTheDocument();
  });

  it('shows a lost sandbox nobody deleted as lost', () => {
    const { container } = render(<SandboxStatus sandbox={sandbox({ state: 'lost' })} />);
    expect(container).toHaveTextContent(/^lost$/);
  });

  it('still names a running sandbox’s activity', () => {
    const { container } = render(
      <SandboxStatus sandbox={sandbox({ state: 'running', active_attempt_count: 2 })} />,
    );
    expect(container).toHaveTextContent(/^Executing2 active turns$/);
  });
});
