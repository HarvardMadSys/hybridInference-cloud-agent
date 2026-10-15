'use client';

import { useT } from '@/components/providers';
import {
  McpConnectionsSection,
  type McpAuthorizationResult,
} from '@/components/features/agents/mcp/McpConnectionsSection';
import { SkillsSection } from '@/components/features/agents/skills/SkillsSection';
import { SettingsPage } from './SettingsPage';

export function PluginsSettingsView({
  authorizationResult,
}: {
  authorizationResult?: McpAuthorizationResult;
}) {
  const t = useT();
  return (
    <SettingsPage title={t('settings.plugins')} description={t('settings.pluginsDescription')}>
      <div id="settings-mcp" className="ui-card mt-6 bg-surface p-3 sm:p-4">
        <McpConnectionsSection authorizationResult={authorizationResult} />
      </div>
      <div id="settings-skills">
        <SkillsSection />
      </div>
    </SettingsPage>
  );
}
