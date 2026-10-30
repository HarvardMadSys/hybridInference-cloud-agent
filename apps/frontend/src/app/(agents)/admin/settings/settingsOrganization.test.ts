import { describe, expect, it } from 'vitest';

import type { AdminSettingDefinition } from '@/lib/api/adminSettings';

import {
  applicationCategory,
  applicationCategoryName,
  applicationSettingGroup,
} from './settingsOrganization';

function definition(key: string, category: string): AdminSettingDefinition {
  return {
    key,
    category,
    label: key,
    description: '',
    kind: 'string',
    default: '',
    secret: false,
  };
}

describe('application settings organization', () => {
  it.each([
    ['Sign-in', 'Identity & access'],
    ['Security', 'Identity & access'],
    ['Source control', 'Source control'],
    ['Publication', 'Source control'],
    ['Attachments', 'Content'],
    ['Skills', 'Content'],
    ['Task titles', 'Content'],
    ['Operations', 'Operations'],
    ['Goals', 'Operations'],
    ['Database tools', 'Operations'],
    ['Compute', 'Compute'],
    ['Execution', 'Compute'],
    ['MCP', 'MCP'],
    ['Appearance', 'Appearance'],
    ['Sandbox archives', 'Sandbox archives'],
  ])('places registry category %s on %s', (category, expected) => {
    expect(applicationCategory(definition('future.setting', category))).toBe(expected);
    expect(applicationCategoryName(category)).toBe(expected);
  });

  it('keeps future categories and settings visible without inferring grouping from their names', () => {
    const field = definition('future.request_timeout_s', 'Future / category ✓');
    expect(applicationCategory(field)).toBe('Future / category ✓');
    expect(applicationSettingGroup(field)).toBe('Future / category ✓');
    expect(applicationCategoryName('constructor')).toBe('constructor');
  });

  it('puts uncategorized settings in General', () => {
    const field = definition('future.setting', '');
    expect(applicationCategory(field)).toBe('General');
    expect(applicationSettingGroup(field)).toBe('General');
  });

  it('groups provider credentials together across source control and publication', () => {
    const app = definition('github_app_id', 'Source control');
    const publication = definition('github_publish_token', 'Publication');
    expect(applicationCategory(app)).toBe(applicationCategory(publication));
    expect(applicationSettingGroup(app)).toBe('GitHub');
    expect(applicationSettingGroup(publication)).toBe('GitHub');
  });

  it.each([
    ['web_base_url', 'Public addresses'],
    ['public_api_url', 'Public addresses'],
    ['attachments.max_file_bytes', 'Attachments'],
    ['task_titles.max_chars', 'Task titles'],
    ['default_execution_profile', 'Sandbox execution'],
    ['sandbox_manager_token', 'Sandbox execution'],
    ['site.org_name', 'Branding'],
    ['log_level', 'Logging'],
    ['mcp_allow_private_targets', 'MCP connections'],
  ])('organizes %s into its related group', (key, group) => {
    const field = definition(key, 'General');
    expect(applicationSettingGroup(field)).toBe(group);
  });

  it('keeps related keys together regardless of whether they are read only', () => {
    const field = definition('source_control_encryption_key', 'Security');
    expect(applicationSettingGroup(field)).toBe('Signing and encryption keys');
    expect(applicationSettingGroup({ ...field, read_only: true })).toBe(
      'Signing and encryption keys',
    );
  });
});
