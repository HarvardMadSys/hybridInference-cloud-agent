import type { AdminSettingDefinition } from '@/lib/api/adminSettings';

const CATEGORY_NAMES = new Map([
  ['Accounts', 'Identity & access'],
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
  keys: string[];
}

// Keys are listed explicitly so a new setting remains visible until its
// presentation has been considered. Timings and transport tuning stay beside
// their related configuration instead of becoming separate settings pages.
const SETTING_GROUPS: SettingGroup[] = [
  {
    title: 'Branding',
    keys: ['site.app_name', 'site.org_name', 'site.org_url'],
  },
  {
    title: 'Navigation links',
    keys: ['site.status_url', 'site.docs_url', 'site.contact_url'],
  },
  {
    title: 'Public addresses',
    keys: ['web_base_url', 'public_api_url'],
  },
  {
    title: 'Signing and encryption keys',
    keys: [
      'control_signing_secret',
      'source_control_encryption_key',
      'compute_encryption_key',
    ],
  },
  {
    title: 'Session security',
    keys: ['session_cookie_secure', 'trust_proxy_headers'],
  },
  {
    title: 'Repository access',
    keys: ['repo_allowlist'],
  },
  {
    title: 'GitHub',
    keys: [
      'github_app_id',
      'github_app_client_id',
      'github_app_client_secret',
      'github_app_private_key',
      'github_app_install_url',
      'github_publish_token',
      'github_api_url',
      'github_reload_interval_s',
      'github_repository_cache_s',
      'github_request_timeout_s',
    ],
  },
  {
    title: 'GitLab',
    keys: [
      'gitlab_client_id',
      'gitlab_client_secret',
      'gitlab_redirect_uri',
      'gitlab_request_timeout_s',
    ],
  },
  {
    title: 'Pull requests',
    keys: [
      'publish_base_branch',
      'git_author_name',
      'git_author_email',
      'publish_poll_seconds',
      'publish_idle_seconds',
      'publish_git_timeout_s',
      'publish_pr_timeout_s',
    ],
  },
  {
    title: 'MCP catalog',
    keys: [
      'mcp_registry_url',
      'mcp_catalog_sync_seconds',
      'mcp_catalog_retry_seconds',
      'mcp_catalog_sync_timeout_s',
      'mcp_catalog_request_timeout_s',
      'mcp_catalog_storage_timeout_s',
    ],
  },
  {
    title: 'MCP connections',
    keys: [
      'mcp_public_base_url',
      'mcp_request_timeout_s',
      'mcp_connect_timeout_s',
      'mcp_metadata_timeout_s',
      'mcp_allow_private_targets',
      'mcp_allow_plain_http',
    ],
  },
  {
    title: 'GitHub sign-in',
    keys: ['mcp_github_client_id', 'mcp_github_client_secret'],
  },
  {
    title: 'Compute services',
    keys: [
      'compute.cloudlab_api_url',
      'compute.registration_url',
      'compute.poll_seconds',
      'compute.observation_seconds',
    ],
  },
  {
    title: 'Sandbox execution',
    keys: [
      'default_execution_profile',
      'sandbox_manager_token',
      'sandbox_manager_url_allowed_hosts',
    ],
  },
  {
    title: 'Logging',
    keys: ['log_level', 'log_format'],
  },
  {
    title: 'Alerts',
    keys: [
      'alert_webhook_url',
      'queue_alert_after_s',
      'queue_monitor_interval_s',
      'queue_alert_cooldown_s',
    ],
  },
  {
    title: 'Job recovery',
    keys: ['max_attempts', 'residency_ttl_s'],
  },
  {
    title: 'Usage and history',
    keys: [
      'ledger_idle_s',
      'snapshot_idle_s',
      'snapshot_capture_interval_s',
      'capacity_history_retention_s',
    ],
  },
  {
    title: 'Goals',
    keys: [
      'goals.arm_after_seconds',
      'goals.stalled_after_seconds',
      'goals.max_wakeup_attempts',
      'goals.wakeup_backoff_seconds',
      'goals.wakeup_backoff_max_seconds',
    ],
  },
  {
    title: 'Database tools',
    keys: ['pgadmin_url', 'pgadmin_public_url'],
  },
  {
    title: 'Attachments',
    keys: ['attachments.max_file_bytes', 'attachments.max_total_bytes', 'attachments.max_files'],
  },
  {
    title: 'Skills',
    keys: [
      'skills.max_skill_bytes',
      'skills.max_selection_bytes',
      'skills.max_selected',
      'skills.max_library',
    ],
  },
  {
    title: 'Task titles',
    keys: [
      'task_titles.max_chars',
    ],
  },
  {
    title: 'Sign-up and approval',
    keys: [
      'accounts.signup_enabled',
      'accounts.require_email_verification',
      'accounts.approval_policy',
      'accounts.signup_admin_notify',
      'accounts.signup_notify_emails',
      'accounts.terms_url',
      'accounts.terms_text',
      'accounts.admin_emails',
      'accounts.session_ttl_days',
    ],
  },
  {
    title: 'Sign-in and sign-up limits',
    keys: [
      'accounts.login_per_15min',
      'accounts.login_account_per_15min',
      'accounts.login_ip_per_hour',
      'accounts.signup_per_hour',
      'accounts.signup_per_day',
      'accounts.signup_require_resolved_ip',
      'accounts.unresolved_signup_per_hour',
      'accounts.unresolved_signup_per_day',
    ],
  },
  {
    title: 'Account email',
    keys: [
      'accounts.email_provider',
      'accounts.smtp_host',
      'accounts.smtp_port',
      'accounts.smtp_username',
      'accounts.smtp_password',
      'accounts.smtp_from',
      'accounts.smtp_security',
      'accounts.tencent_ses_region',
      'accounts.tencent_ses_from_email',
      'accounts.tencent_ses_from_name',
      'accounts.tencent_ses_template_verification',
      'accounts.tencent_ses_template_password_reset',
      'accounts.tencent_ses_template_approval',
      'accounts.tencent_ses_template_rejection',
      'accounts.tencent_ses_template_admin_signup',
      'accounts.tencent_ses_template_notice',
    ],
  },
  {
    title: 'Phone sign-in',
    keys: [
      'accounts.sms_enabled',
      'accounts.sms_provider',
      'accounts.sms_default_country_code',
      'accounts.sms_allowed_prefixes',
      'accounts.sms_code_ttl_s',
      'accounts.sms_send_cooldown_s',
      'accounts.sms_send_per_phone_hour',
      'accounts.sms_send_per_phone_day',
      'accounts.sms_send_per_ip_hour',
      'accounts.sms_send_per_ip_day',
      'accounts.sms_max_attempts',
      'accounts.sms_verify_per_phone_15min',
      'accounts.sms_verify_per_ip_hour',
      'accounts.tencent_sms_region',
      'accounts.tencent_sms_app_id',
      'accounts.tencent_sms_sign_name',
      'accounts.tencent_sms_template_id',
    ],
  },
  {
    title: 'Tencent Cloud credentials',
    keys: ['accounts.tencent_secret_id', 'accounts.tencent_secret_key', 'accounts.tencent_token'],
  },
  {
    title: 'Captcha',
    keys: ['accounts.turnstile_site_key', 'accounts.turnstile_secret_key'],
  },
  {
    title: 'Deletion archive storage',
    keys: [
      'sandbox_archive.bucket',
      'sandbox_archive.region',
      'sandbox_archive.prefix',
      'sandbox_archive.endpoint',
    ],
  },
];

const SETTINGS_BY_KEY = new Map<string, string>(
  SETTING_GROUPS.flatMap(({ title, keys }) => keys.map((key) => [key, title] as const)),
);

export function applicationCategoryName(category: string): string {
  return CATEGORY_NAMES.get(category) ?? (category || 'General');
}

export function applicationCategory(definition: AdminSettingDefinition): string {
  return applicationCategoryName(definition.category);
}

export function applicationSettingGroup(definition: AdminSettingDefinition): string {
  return SETTINGS_BY_KEY.get(definition.key) ?? (definition.category || 'General');
}
