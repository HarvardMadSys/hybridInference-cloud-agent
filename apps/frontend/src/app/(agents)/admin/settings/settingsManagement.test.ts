import { describe, expect, it } from 'vitest';

import type { ManagementCatalog, ManagementStatus } from '@/lib/api/adminManagement';
import { managementView } from './settingsManagement';

const status = (reporting: number, adopted: number): ManagementStatus => ({
  settings: {
    saved_revision: 7,
    reporting,
    adopted,
    restart_required: { mcp_allow_plain_http: ['mcp@cp-1'] },
  },
  instances: [],
  unidentified_sessions: [],
  hosts: [],
});

const catalog = {
  methods: [],
  apply_modes: [],
  items: [
    {
      id: 'x',
      setting_keys: ['mcp_allow_plain_http'],
      sources: [{ kind: 'settings', reader: 'r', meaning: 'saved setting' }],
    },
  ],
} as unknown as ManagementCatalog;

describe('what the settings page may claim', () => {
  it('shows nothing against a control plane without management reports', () => {
    expect(managementView(null, null)).toBeNull();
  });

  it('counts adoption and never reads silence as adopted', () => {
    expect(managementView(null, status(3, 2))?.adoption).toBe(
      'Saved. 2 of 3 running processes use these settings so far.',
    );
    expect(managementView(null, status(3, 3))?.adoption).toBe(
      'Saved. All 3 running processes use these settings.',
    );
    expect(managementView(null, status(0, 0))?.adoption).toContain('unknown');
  });

  it('names the restart a field waits for in words, never as a process id', () => {
    const note = managementView(catalog, status(1, 1))?.fieldNote('mcp_allow_plain_http');
    expect(note).toBe(' Still waiting for a restart of the MCP listener on cp-1.');
    expect(note).not.toMatch(/mcp@|revision|saved setting/);
    expect(managementView(catalog, status(1, 1))?.fieldNote('other')).toBe('');
  });
});
