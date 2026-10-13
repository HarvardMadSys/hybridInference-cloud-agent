import { describe, expect, it } from 'vitest';

import type { HostSettingDefinition } from '@/lib/api/admin';
import type { AgentAdminSettings } from '@/lib/api/adminSettings';

import { buildSettingsSearchItems, searchSettingsItems } from './settingsSearchIndex';

const snapshot: AgentAdminSettings = {
  revision: 1,
  definitions: [
    {
      key: 'github_publish_token',
      label: 'GitHub publication token',
      description: 'Credential used to publish pull requests.',
      category: 'Publication',
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
      key: 'future.setting',
      label: 'Future limit',
      description: 'A newly introduced setting.',
      category: 'Future / category ✓',
      kind: 'integer',
      default: 10,
      secret: false,
    },
  ],
  values: { github_publish_token: null, 'future.setting': 73129 },
  secret_configured: { github_publish_token: true },
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
  scope: 'broker',
};

function items(over: Partial<Parameters<typeof buildSettingsSearchItems>[0]> = {}) {
  return buildSettingsSearchItems({
    snapshot,
    hostDefinitions: [hostDefinition],
    runtimes: ['claude-code', 'future-agent'],
    archiveAvailable: true,
    applicationTab: 'application-Source%20control',
    ...over,
  });
}

describe('settings search', () => {
  it('searches merged and original application categories, related groups, and descriptions', () => {
    const catalog = items();
    const token = catalog.find((item) => item.applicationKey === 'github_publish_token');
    expect(searchSettingsItems(catalog, 'source control publication')).toEqual([token]);
    expect(searchSettingsItems(catalog, 'github credential requests')).toEqual([token]);
    expect(token).toMatchObject({
      tab: 'application-Source%20control',
      targetId: 'application-setting-github_publish_token',
      section: 'Source control',
    });
  });

  it('normalizes case, underscores, and punctuation and requires every query token', () => {
    const catalog = items();
    expect(searchSettingsItems(catalog, '  GITHUB_PUBLISH.TOKEN  ')).toEqual([
      catalog.find((item) => item.id === 'application:github_publish_token'),
    ]);
    expect(searchSettingsItems(catalog, 'AGENT_TERMINAL_MAX_SESSIONS sandbox')).toEqual([
      catalog.find((item) => item.id === 'host:terminal_max_sessions'),
    ]);
    expect(searchSettingsItems(catalog, 'github unmatched-term')).toEqual([]);
    expect(searchSettingsItems(catalog, '   ')).toEqual([]);
    expect(searchSettingsItems(catalog, '__...--')).toEqual([]);
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

  it('routes deployment, host, agent version, archive, and CloudLab metadata to their controls', () => {
    const catalog = items();
    expect(searchSettingsItems(catalog, 'agent_database_url')).toEqual([
      expect.objectContaining({
        tab: 'application-Source%20control',
        section: 'Deployment setup',
        targetId: 'application-bootstrap-AGENT_DATABASE_URL',
      }),
    ]);
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
