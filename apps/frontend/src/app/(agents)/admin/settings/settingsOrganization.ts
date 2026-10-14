import type { AdminSettingDefinition } from '@/lib/api/adminSettings';

export type ApplicationSettingPriority = 'important' | 'advanced';

const CATEGORY_NAMES = new Map([
  ['Sign-in', 'Identity & access'],
  ['Security', 'Identity & access'],
  ['Publication', 'Source control'],
  ['Attachments', 'Content'],
  ['Skills', 'Content'],
  ['Task titles', 'Content'],
  ['Goals', 'Operations'],
  ['Database tools', 'Operations'],
  ['Execution', 'Compute'],
]);

interface SettingGroup {
  title: string;
  important: string[];
  advanced: string[];
}

// Keys are listed explicitly so a new setting remains visible until its
// presentation has been considered. Timings and transport tuning stay beside
// their related configuration instead of becoming separate settings pages.
const SETTING_GROUPS: SettingGroup[] = [
  {
    title: 'Branding',
    important: ['site.app_name', 'site.org_name', 'site.org_url', 'site.feature_rag'],
    advanced: [],
  },
  {
    title: 'Navigation links',
    important: ['site.status_url', 'site.docs_url', 'site.gateway_url'],
    advanced: [],
  },
  {
    title: 'Gateway connection',
    important: ['gateway_base_url', 'gateway_dispatch_token'],
    advanced: [
      'gateway_grant_base_path',
      'gateway_grant_binding_id',
      'gateway_request_timeout_s',
      'gateway_connect_timeout_s',
    ],
  },
  {
    title: 'Sign-in',
    important: [
      'gateway_authorize_url',
      'session_redirect_uri',
      'web_base_url',
      'gateway_issuer',
      'gateway_jwks_url',
    ],
    advanced: [],
  },
  {
    title: 'Signing and encryption keys',
    important: [
      'session_signing_secret',
      'control_signing_secret',
      'source_control_encryption_key',
      'compute_encryption_key',
    ],
    advanced: [],
  },
  {
    title: 'Session security',
    important: ['session_cookie_secure'],
    advanced: ['trust_proxy_headers'],
  },
  {
    title: 'Repository access',
    important: ['repo_allowlist'],
    advanced: [],
  },
  {
    title: 'GitHub',
    important: [
      'github_app_id',
      'github_app_client_id',
      'github_app_client_secret',
      'github_app_private_key',
      'github_app_install_url',
      'github_publish_token',
    ],
    advanced: [
      'github_api_url',
      'github_reload_interval_s',
      'github_repository_cache_s',
      'github_request_timeout_s',
    ],
  },
  {
    title: 'GitLab',
    important: ['gitlab_client_id', 'gitlab_client_secret', 'gitlab_redirect_uri'],
    advanced: ['gitlab_request_timeout_s'],
  },
  {
    title: 'Pull requests',
    important: ['publish_base_branch', 'git_author_name', 'git_author_email'],
    advanced: [
      'publish_poll_seconds',
      'publish_idle_seconds',
      'publish_git_timeout_s',
      'publish_pr_timeout_s',
    ],
  },
  {
    title: 'Archive after merge',
    important: ['pr_archive_enabled'],
    advanced: [
      'pr_archive_poll_s',
      'pr_archive_timeout_s',
      'pr_archive_batch_size',
      'pr_archive_retry_max_s',
      'pr_archive_max_failures',
    ],
  },
  {
    title: 'MCP catalog',
    important: ['mcp_registry_url'],
    advanced: [
      'mcp_catalog_sync_seconds',
      'mcp_catalog_retry_seconds',
      'mcp_catalog_sync_timeout_s',
      'mcp_catalog_request_timeout_s',
      'mcp_catalog_storage_timeout_s',
    ],
  },
  {
    title: 'MCP connections',
    important: ['mcp_public_base_url'],
    advanced: [
      'mcp_request_timeout_s',
      'mcp_connect_timeout_s',
      'mcp_metadata_timeout_s',
      'mcp_allow_private_targets',
      'mcp_allow_plain_http',
    ],
  },
  {
    title: 'Compute services',
    important: ['compute.cloudlab_api_url', 'compute.registration_url'],
    advanced: ['compute.poll_seconds', 'compute.observation_seconds'],
  },
  {
    title: 'Sandbox execution',
    important: ['default_execution_profile'],
    advanced: ['sandbox_manager_token', 'sandbox_manager_url_allowed_hosts'],
  },
  {
    title: 'Logging',
    important: [],
    advanced: ['log_level', 'log_format'],
  },
  {
    title: 'Alerts',
    important: [
      'alert_webhook_url',
      'oncall_user_id',
      'oncall_dispatch_token',
      'queue_alert_after_s',
    ],
    advanced: ['queue_monitor_interval_s', 'queue_alert_cooldown_s'],
  },
  {
    title: 'Job recovery',
    important: ['max_attempts'],
    advanced: ['residency_ttl_s', 'grant_renewal_concurrency'],
  },
  {
    title: 'Usage and history',
    important: [],
    advanced: [
      'ledger_refresh_concurrency',
      'ledger_refresh_timeout_s',
      'ledger_idle_s',
      'snapshot_idle_s',
      'snapshot_capture_interval_s',
      'capacity_history_retention_s',
    ],
  },
  {
    title: 'Goals',
    important: ['goals.arm_after_seconds', 'goals.stalled_after_seconds'],
    advanced: [
      'goals.max_wakeup_attempts',
      'goals.wakeup_backoff_seconds',
      'goals.wakeup_backoff_max_seconds',
    ],
  },
  {
    title: 'Database tools',
    important: [],
    advanced: ['pgadmin_url', 'pgadmin_public_url'],
  },
  {
    title: 'Attachments',
    important: [
      'attachments.max_file_bytes',
      'attachments.max_total_bytes',
      'attachments.max_files',
    ],
    advanced: [],
  },
  {
    title: 'Skills',
    important: [
      'skills.max_skill_bytes',
      'skills.max_selection_bytes',
      'skills.max_selected',
      'skills.max_library',
    ],
    advanced: [],
  },
  {
    title: 'Task titles',
    important: ['task_titles.max_chars'],
    advanced: [
      'task_titles.input_max_chars',
      'task_titles.timeout_seconds',
      'task_titles.total_timeout_seconds',
      'task_titles.max_attempts',
      'task_titles.max_tokens',
    ],
  },
  {
    title: 'Deletion archive storage',
    important: ['sandbox_archive.bucket', 'sandbox_archive.region', 'sandbox_archive.prefix'],
    advanced: ['sandbox_archive.endpoint'],
  },
];

const SETTINGS_BY_KEY = new Map<string, { group: string; priority: ApplicationSettingPriority }>(
  SETTING_GROUPS.flatMap(({ title, important, advanced }) => [
    ...important.map((key) => [key, { group: title, priority: 'important' as const }] as const),
    ...advanced.map((key) => [key, { group: title, priority: 'advanced' as const }] as const),
  ]),
);

export function applicationCategoryName(category: string): string {
  return CATEGORY_NAMES.get(category) ?? (category || 'General');
}

export function applicationCategory(definition: AdminSettingDefinition): string {
  return applicationCategoryName(definition.category);
}

export function applicationSettingGroup(definition: AdminSettingDefinition): string {
  return SETTINGS_BY_KEY.get(definition.key)?.group ?? (definition.category || 'General');
}

export function applicationSettingPriority(
  definition: AdminSettingDefinition,
): ApplicationSettingPriority {
  const organization = SETTINGS_BY_KEY.get(definition.key);
  if (!organization) return 'important';
  // Configuring an identity/key is ordinary setup. Once the backend locks it,
  // move its migration information into Advanced without hiding missing setup.
  return definition.read_only ? 'advanced' : organization.priority;
}
