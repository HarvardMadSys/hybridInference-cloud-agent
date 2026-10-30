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
      'Saved as revision 7. In use by 2 of 3 reporting processes.',
    );
    expect(managementView(null, status(0, 0))?.adoption).toContain('unknown');
  });

  it('names where a field is stored and which restart it waits for', () => {
    const note = managementView(catalog, status(1, 1))?.fieldNote('mcp_allow_plain_http');
    expect(note).toContain('Stored as: saved setting.');
    expect(note).toContain('Waiting for a restart of: mcp@cp-1.');
    expect(managementView(catalog, status(1, 1))?.fieldNote('other')).toBe('');
  });
});
