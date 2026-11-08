// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { AdminSandbox } from '@/lib/api/adminSandboxes';
import { SandboxIncidentBadge } from './SandboxIncidentBadge';

function sandbox(fields: Partial<AdminSandbox>): AdminSandbox {
  return fields as AdminSandbox;
}

describe('SandboxIncidentBadge', () => {
  it('names the newest incident of the day, so a sandbox in trouble stands out', () => {
    render(
      <SandboxIncidentBadge
        sandbox={sandbox({
          last_incident: 'out_of_memory.repeated',
          last_incident_at: '2026-11-05T10:00:00Z',
        })}
      />,
    );
    expect(screen.getByText('Incident: out of memory repeated')).toHaveAttribute(
      'title',
      '2026-11-05T10:00:00Z',
    );
  });

  it('shows nothing for a sandbox with no recent incident', () => {
    const { container } = render(
      <SandboxIncidentBadge sandbox={sandbox({ last_incident: null })} />,
    );
    expect(container).toBeEmptyDOMElement();
  });
});
