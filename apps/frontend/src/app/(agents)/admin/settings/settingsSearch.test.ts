import { describe, expect, it } from 'vitest';

import type { HostSettingDefinition } from '@/lib/api/admin';
import type { AgentAdminSettings } from '@/lib/api/adminSettings';

import { buildSettingsSearchItems, searchSettingsItems } from './settingsSearchIndex';

const snapshot: AgentAdminSettings = {
  revision: 1,
  definitions: [
    {
      key: 'control_signing_secret',
      label: 'Control signing secret',
      description: 'Credential used to sign control requests.',
      category: 'Security',
      kind: 'string',
      default: 'private-default-value',
      secret: true,
    },
    {
      key: 'sandbox_archive.endpoint',
      label: 'Archive endpoint',
      description: 'S3-compatible service URL.',
      category: 'Sandbox archives',
      kind: 'string',
      default: '',
      secret: false,
    },
    {
      key: 'runtime_registry_url',
      label: 'Agent package registry',
      description: 'The npm registry agent versions come from.',
      category: 'Agent versions',
      kind: 'string',
      default: '',
      secret: false,
    },
    {
      key: 'future.setting',
      label: 'Future limit',
      description: 'A newly introduced setting.',
      category: 'Future / category ✓',
      kind: 'integer',
      default: 10,
      secret: false,
    },
  ],
  values: { control_signing_secret: null, 'future.setting': 73129 },
  secret_configured: { control_signing_secret: true },
  bootstrap: [
    {
      key: 'AGENT_DATABASE_URL',
      label: 'Database connection',
      description: 'Supplied before the service starts.',
    },
  ],
};

const hostDefinition: HostSettingDefinition = {
  key: 'terminal_max_sessions',
  env: 'AGENT_TERMINAL_MAX_SESSIONS',
  label: 'Terminal sessions',
  description: 'Maximum terminals opened in one sandbox.',
  unit: 'count',
  default: 77,
  minimum: 1,
  maximum: 100,
  scope: 'sandbox_manager',
};

function items(over: Partial<Parameters<typeof buildSettingsSearchItems>[0]> = {}) {
  return buildSettingsSearchItems({
    snapshot,
    hostDefinitions: [hostDefinition],
    runtimes: ['claude-code', 'future-agent'],
    archiveAvailable: true,
    ...over,
  });
}

describe('settings search', () => {
  it('searches merged and original application categories, related groups, and descriptions', () => {
    const catalog = items();
    const token = catalog.find((item) => item.applicationKey === 'control_signing_secret');
    expect(searchSettingsItems(catalog, 'identity access security')).toEqual([token]);
    expect(searchSettingsItems(catalog, 'encryption keys credential requests')).toEqual([token]);
    expect(token).toMatchObject({
      tab: 'application-Identity%20%26%20access',
      targetId: 'application-setting-control_signing_secret',
      section: 'Identity & access',
    });
  });

  it('normalizes case, underscores, and punctuation and requires every query token', () => {
    const catalog = items();
    expect(searchSettingsItems(catalog, '  CONTROL_SIGNING.SECRET  ')).toEqual([
      catalog.find((item) => item.id === 'application:control_signing_secret'),
    ]);
    expect(searchSettingsItems(catalog, 'AGENT_TERMINAL_MAX_SESSIONS sandbox')).toEqual([
      catalog.find((item) => item.id === 'host:terminal_max_sessions'),
    ]);
    expect(searchSettingsItems(catalog, 'github unmatched-term')).toEqual([]);
    expect(searchSettingsItems(catalog, '   ')).toEqual([]);
    expect(searchSettingsItems(catalog, '__...--')).toEqual([]);
  });

  it('routes the agent package registry to the Agent versions tab', () => {
    expect(searchSettingsItems(items(), 'npm registry')).toEqual([
      expect.objectContaining({
        applicationKey: 'runtime_registry_url',
        tab: 'versions',
        section: 'Agent versions',
      }),
    ]);
  });

  it('keeps new definitions searchable and routes archive storage into Sandbox', () => {
    const catalog = items();
    expect(searchSettingsItems(catalog, 'newly introduced')).toEqual([
      expect.objectContaining({
        applicationKey: 'future.setting',
        tab: 'application-Future%20%2F%20category%20%E2%9C%93',
      }),
    ]);
    expect(searchSettingsItems(catalog, 'sandbox_archive endpoint')).toEqual([
      expect.objectContaining({
        tab: 'sandbox',
        section: 'Sandbox',
        targetId: 'application-setting-sandbox_archive.endpoint',
      }),
    ]);
  });

  it('routes host, agent version, archive, and CloudLab metadata to their controls', () => {
    const catalog = items();
    // Deployment notes are not settings; they never become a search destination.
    expect(searchSettingsItems(catalog, 'agent_database_url')).toEqual([]);
    expect(searchSettingsItems(catalog, 'Database connection')).toEqual([]);
    expect(catalog.some((item) => item.id.startsWith('bootstrap:'))).toBe(false);
    expect(searchSettingsItems(catalog, 'terminal_max_sessions')).toEqual([
      expect.objectContaining({ tab: 'sandbox', targetId: 'host-setting-terminal_max_sessions' }),
    ]);
    expect(searchSettingsItems(catalog, 'claude code')).toEqual([
      expect.objectContaining({ tab: 'versions', targetId: 'agent-version-claude-code' }),
    ]);
    expect(searchSettingsItems(catalog, 'future agent')).toEqual([
      expect.objectContaining({ tab: 'versions', targetId: 'agent-version-future-agent' }),
    ]);
    expect(searchSettingsItems(catalog, 'multipart_part_bytes uploaded')).toEqual([
      expect.objectContaining({ tab: 'sandbox', targetId: 'archive-setting-multipart_part_bytes' }),
    ]);
    expect(searchSettingsItems(catalog, 'cloudlab profile_name')).toEqual([
      expect.objectContaining({ tab: 'cloudlab', targetId: 'cloudlab-setting-profile_name' }),
    ]);
  });

  it('makes CloudLab discoverable before loading and includes archive fields only when available', () => {
    const catalog = items({
      snapshot: null,
      hostDefinitions: [],
      runtimes: [],
      archiveAvailable: false,
    });
    expect(catalog).toHaveLength(6);
    expect(catalog.every((item) => item.tab === 'cloudlab')).toBe(true);
    expect(searchSettingsItems(catalog, 'ssh_public_key')).toEqual([
      expect.objectContaining({ targetId: 'cloudlab-setting-ssh_public_key' }),
    ]);
    expect(searchSettingsItems(catalog, 'aws secret')).toEqual([]);
    expect(new Set(items().map((item) => item.id)).size).toBe(items().length);
  });

  it('excludes saved values, defaults, and secret configuration from the catalog', () => {
    const catalog = items();
    for (const query of ['private-default-value', '73129', '77']) {
      expect(searchSettingsItems(catalog, query)).toEqual([]);
    }
    for (const item of catalog) {
      expect(item).not.toHaveProperty('value');
      expect(item).not.toHaveProperty('default');
      expect(item).not.toHaveProperty('secret_configured');
    }
    expect(JSON.stringify(catalog)).not.toContain('private-default-value');
  });
});
