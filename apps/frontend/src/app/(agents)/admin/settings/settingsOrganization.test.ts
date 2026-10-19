import { describe, expect, it } from 'vitest';

import type { AdminSettingDefinition } from '@/lib/api/adminSettings';

import {
  applicationCategory,
  applicationCategoryName,
  applicationSettingGroup,
  applicationSettingPriority,
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
    ['Gateway', 'Gateway'],
    ['MCP', 'MCP'],
    ['Appearance', 'Appearance'],
    ['Sandbox archives', 'Sandbox archives'],
  ])('places registry category %s on %s', (category, expected) => {
    expect(applicationCategory(definition('future.setting', category))).toBe(expected);
    expect(applicationCategoryName(category)).toBe(expected);
  });

  it('keeps future categories and settings visible without inferring priority from their names', () => {
    const field = definition('future.request_timeout_s', 'Future / category ✓');
    expect(applicationCategory(field)).toBe('Future / category ✓');
    expect(applicationSettingGroup(field)).toBe('Future / category ✓');
    expect(applicationSettingPriority(field)).toBe('important');
    expect(applicationCategoryName('constructor')).toBe('constructor');
  });

  it('puts uncategorized settings in General', () => {
    const field = definition('future.setting', '');
    expect(applicationCategory(field)).toBe('General');
    expect(applicationSettingGroup(field)).toBe('General');
    expect(applicationSettingPriority(field)).toBe('important');
  });

  it('groups provider credentials together across source control and publication', () => {
    const app = definition('github_app_id', 'Source control');
    const publication = definition('github_publish_token', 'Publication');
    expect(applicationCategory(app)).toBe(applicationCategory(publication));
    expect(applicationSettingGroup(app)).toBe('GitHub');
    expect(applicationSettingGroup(publication)).toBe('GitHub');
  });

  it.each([
    ['gateway_base_url', 'Gateway connection', 'important'],
    ['gateway_request_timeout_s', 'Gateway connection', 'advanced'],
    ['attachments.max_file_bytes', 'Attachments', 'important'],
    ['task_titles.timeout_seconds', 'Task titles', 'advanced'],
    ['default_execution_profile', 'Sandbox execution', 'important'],
    ['sandbox_manager_token', 'Sandbox execution', 'advanced'],
    ['site.feature_rag', 'Branding', 'important'],
    ['log_level', 'Logging', 'advanced'],
    ['mcp_allow_private_targets', 'MCP connections', 'advanced'],
  ])('organizes %s into its related group and priority', (key, group, priority) => {
    const field = definition(key, 'General');
    expect(applicationSettingGroup(field)).toBe(group);
    expect(applicationSettingPriority(field)).toBe(priority);
  });

  it('keeps initial key setup important and locked migration information advanced', () => {
    const field = definition('source_control_encryption_key', 'Security');
    expect(applicationSettingPriority(field)).toBe('important');
    expect(applicationSettingPriority({ ...field, read_only: true })).toBe('advanced');
  });
});
