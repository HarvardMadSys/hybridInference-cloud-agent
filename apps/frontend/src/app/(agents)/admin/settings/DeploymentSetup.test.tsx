// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { DeploymentSetupDefinition } from '@/lib/api/adminSettings';

import DeploymentSetup, {
  deploymentHashTarget,
  deploymentTargetId,
  resolveDeploymentTarget,
} from './DeploymentSetup';

const legacy: DeploymentSetupDefinition[] = [
  {
    key: 'AGENT_DATABASE_URL',
    label: 'Database connection',
    description: 'Database connection supplied before the service starts.',
  },
];

function definitions(over: Partial<DeploymentSetupDefinition> = {}): DeploymentSetupDefinition[] {
  return [
    {
      id: 'database',
      key: 'AGENT_DATABASE_URL',
      label: 'Database connection',
      description: 'Deployment-managed connection.',
      management: 'deployment',
      requirement: 'required',
      state: 'configured',
      actions: [{ target: 'database', label: 'Open Admin Database' }],
      ...over,
    },
  ];
}

afterEach(cleanup);

describe('Deployment setup', () => {
  it('renders an old API shape with the legacy anchor and no invented status', () => {
    render(<DeploymentSetup definitions={legacy} />);
    const row = document.getElementById('application-bootstrap-AGENT_DATABASE_URL');
    expect(row).not.toBeNull();
    expect(screen.getByText('Database connection')).toBeVisible();
    expect(screen.getByText('AGENT_DATABASE_URL')).toBeVisible();
    for (const label of [
      'Configuration present',
      'Not configured',
      'Short credential',
      'Using session-secret fallback',
      'Not used',
      'Not checked',
    ]) {
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
  });

  it('labels configuration states neutrally and never calls them healthy', () => {
    render(
      <DeploymentSetup
        definitions={[
          { ...definitions()[0], key: 'a', state: 'configured' },
          { ...definitions()[0], key: 'b', state: 'missing' },
          { ...definitions()[0], key: 'c', state: 'weak' },
          { ...definitions()[0], key: 'd', state: 'fallback' },
          { ...definitions()[0], key: 'e', state: 'not_used' },
          { ...definitions()[0], key: 'f', state: 'unknown' },
        ]}
      />,
    );
    for (const label of [
      'Configuration present',
      'Not configured',
      'Short credential',
      'Using session-secret fallback',
      'Not used',
      'Not checked',
    ]) {
      expect(screen.getByText(label)).toBeVisible();
    }
    expect(screen.queryByText(/healthy/i)).not.toBeInTheDocument();
  });

  it('explains the fallback by separating the same value from a real rotation', () => {
    render(<DeploymentSetup definitions={definitions({ state: 'fallback' })} />);
    expect(
      screen.getByText(/set AGENT_SETTINGS_ENCRYPTION_KEY to the exact same bytes/i),
    ).toBeVisible();
    expect(screen.getByText(/A different key is a real rotation/i)).toBeVisible();
    expect(screen.queryByText(/generate a fresh/i)).not.toBeInTheDocument();
  });

  it('does not make an unknown or missing action target clickable', () => {
    render(
      <DeploymentSetup
        definitions={definitions({
          actions: [
            { target: 'https://example.invalid/rotate', label: 'Rotate now' },
            { target: 'made_up_target', label: 'Do something' },
          ],
        })}
      />,
    );
    expect(screen.queryByRole('link', { name: /Rotate now/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Rotate now/ })).not.toBeInTheDocument();
    expect(screen.getByText('Rotate now')).toBeVisible();
    expect(screen.getByText('Do something')).toBeVisible();
    expect(resolveDeploymentTarget('https://example.invalid')).toEqual({ kind: 'unknown' });
  });

  it('maps known names to fixed application destinations', () => {
    expect(resolveDeploymentTarget('github_app_private_key')).toEqual({
      kind: 'setting',
      key: 'github_app_private_key',
    });
    expect(resolveDeploymentTarget('pgadmin_url')).toEqual({
      kind: 'setting',
      key: 'pgadmin_url',
    });
    expect(resolveDeploymentTarget('database')).toEqual({
      kind: 'internal',
      href: '/admin/database',
    });
    expect(resolveDeploymentTarget('compute_setup')).toEqual({
      kind: 'internal',
      href: '/admin/hosts#compute-setup',
    });
    expect(resolveDeploymentTarget('deployment_guide')).toMatchObject({
      kind: 'documentation',
      href: expect.stringMatching(/^https:\/\/github\.com\/HarvardMadSys\//),
    });
    expect(resolveDeploymentTarget('key_rotation_guide')).toMatchObject({
      kind: 'documentation',
    });
  });

  it('links a known setting action to the same-page field', () => {
    const reveal = vi.fn();
    render(
      <DeploymentSetup
        definitions={definitions({
          actions: [{ target: 'pgadmin_url', label: 'Edit pgAdmin URL' }],
        })}
        onRevealSetting={reveal}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Edit pgAdmin URL' }));
    expect(reveal).toHaveBeenCalledWith('pgadmin_url');
  });

  it('suppresses cross-page actions in recovery context and keeps documentation token-free', () => {
    render(
      <DeploymentSetup
        definitions={definitions({
          actions: [
            { target: 'database', label: 'Open Admin Database' },
            { target: 'compute_setup', label: 'Open Compute setup' },
            { target: 'deployment_guide', label: 'View deployment guide' },
          ],
        })}
        recoveryToken="test-recovery-token"
        onRevealSetting={vi.fn()}
      />,
    );
    expect(screen.queryByRole('link', { name: /Open Admin Database/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /Open Compute setup/ })).not.toBeInTheDocument();
    expect(screen.getByText(/Open Admin Database — Gateway sign-in is required/)).toBeVisible();
    expect(screen.getByText(/Open Compute setup — Gateway sign-in is required/)).toBeVisible();

    const docs = screen.getByRole('link', { name: /View deployment guide/ });
    expect(docs).toHaveAttribute('target', '_blank');
    expect(docs.getAttribute('href')).not.toContain('test-recovery-token');
    expect(document.body).not.toHaveTextContent('test-recovery-token');
    expect(window.location.href).not.toContain('test-recovery-token');
  });

  it('resolves new semantic and legacy hashes, including encoded legacy keys', () => {
    const catalog = definitions();
    expect(deploymentTargetId(catalog[0])).toBe('deployment-database');
    expect(deploymentHashTarget('#deployment-database', catalog)).toBe('deployment-database');
    expect(deploymentHashTarget('#deployment-AGENT_DATABASE_URL', catalog)).toBe(
      'deployment-database',
    );
    expect(deploymentHashTarget('#application-bootstrap-AGENT_DATABASE_URL', catalog)).toBe(
      'deployment-database',
    );
    expect(
      deploymentHashTarget(
        '#application-bootstrap-AGENT_PGADMIN_BOOTSTRAP_URL%20%2F%20AGENT_PGADMIN_BOOTSTRAP_DIR',
        [
          {
            key: 'AGENT_PGADMIN_BOOTSTRAP_URL / AGENT_PGADMIN_BOOTSTRAP_DIR',
            label: 'pgAdmin first-time setup',
            description: 'Deployment routing.',
          },
        ],
      ),
    ).toBe('deployment-AGENT_PGADMIN_BOOTSTRAP_URL / AGENT_PGADMIN_BOOTSTRAP_DIR');
    expect(deploymentHashTarget('#deployment-missing', catalog)).toBeNull();
    expect(deploymentHashTarget('#application-setting-title', catalog)).toBeNull();
  });

  it('keeps long variable names wrapping inside the row', () => {
    const key =
      'AGENT_PGADMIN_BOOTSTRAP_URL / AGENT_PGADMIN_BOOTSTRAP_PUBLIC_URL / AGENT_PGADMIN_BOOTSTRAP_DIR';
    render(<DeploymentSetup definitions={definitions({ key })} />);
    expect(screen.getByText(key)).toHaveClass('break-all');
  });
});
