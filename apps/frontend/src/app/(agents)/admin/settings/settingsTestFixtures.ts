import type { AgentAdminSettings } from '@/lib/api/adminSettings';

export function applicationSnapshot(over: Partial<AgentAdminSettings> = {}): AgentAdminSettings {
  return {
    revision: 1,
    definitions: [
      {
        key: 'site.app_name',
        label: 'Application name',
        description: 'Name shown in the application.',
        category: 'Appearance',
        kind: 'string',
        default: 'Cloud Agent',
        secret: false,
      },
      {
        key: 'identity.registration_enabled',
        label: 'Registration enabled',
        description: 'Allow new users to register.',
        category: 'Identity & access',
        kind: 'boolean',
        default: true,
        secret: false,
      },
      {
        key: 'future.maximum',
        label: 'Future limit',
        description: 'A setting introduced by the server.',
        category: 'Future / category ✓',
        kind: 'integer',
        default: 10,
        minimum: 1,
        maximum: 100,
        secret: false,
      },
    ],
    values: {
      'site.app_name': 'Cloud Agent',
      'identity.registration_enabled': true,
      'future.maximum': 10,
    },
    secret_configured: {},
    bootstrap: [],
    ...over,
  };
}

type AccountField = [
  key: string,
  label: string,
  kind: 'string' | 'boolean' | 'integer',
  value: string | number | boolean,
  extra?: { secret?: true; choices?: string[] },
];

// A slice of the server's account registry: both built-in mail transports, phone
// sign-in with its Tencent fields, the credentials they share, and an unrelated
// captcha field. Labels match the registry.
const ACCOUNT_FIELDS: AccountField[] = [
  ['accounts.signup_enabled', 'Open sign-up', 'boolean', true],
  ['accounts.email_provider', 'Email provider', 'string', 'smtp'],
  ['accounts.smtp_host', 'SMTP host', 'string', ''],
  ['accounts.smtp_password', 'SMTP password', 'string', '', { secret: true }],
  ['accounts.smtp_from', 'Sender address', 'string', ''],
  [
    'accounts.tencent_ses_region',
    'Tencent SES region',
    'string',
    'ap-guangzhou',
    { choices: ['ap-guangzhou', 'ap-hongkong'] },
  ],
  ['accounts.tencent_ses_from_email', 'Tencent SES sender address', 'string', ''],
  [
    'accounts.tencent_ses_template_verification',
    'Tencent SES verification template ID',
    'integer',
    0,
  ],
  ['accounts.sms_enabled', 'Phone sign-in', 'boolean', false],
  [
    'accounts.sms_provider',
    'Text message provider',
    'string',
    'none',
    { choices: ['none', 'tencent'] },
  ],
  ['accounts.sms_code_ttl_s', 'Code lifetime (seconds)', 'integer', 300],
  ['accounts.tencent_secret_id', 'Tencent Cloud SecretId', 'string', '', { secret: true }],
  ['accounts.tencent_secret_key', 'Tencent Cloud SecretKey', 'string', '', { secret: true }],
  ['accounts.tencent_sms_app_id', 'Tencent SMS application ID', 'string', ''],
  ['accounts.tencent_sms_template_id', 'Tencent SMS template ID', 'string', ''],
  ['accounts.turnstile_site_key', 'Turnstile site key', 'string', ''],
];

/** The account settings with `values` saved over the registry defaults. */
export function accountProviderSnapshot(
  values: Record<string, string | number | boolean> = {},
  over: Partial<AgentAdminSettings> = {},
): AgentAdminSettings {
  return {
    revision: 1,
    definitions: ACCOUNT_FIELDS.map(([key, label, kind, value, extra = {}]) => ({
      key,
      label,
      description: '',
      category: 'Accounts',
      kind,
      default: value,
      secret: !!extra.secret,
      ...(extra.choices ? { choices: extra.choices } : {}),
    })),
    values: {
      ...Object.fromEntries(
        ACCOUNT_FIELDS.map(([key, , , value, extra]) => [key, extra?.secret ? null : value]),
      ),
      ...values,
    },
    secret_configured: Object.fromEntries(
      ACCOUNT_FIELDS.filter(([, , , , extra]) => extra?.secret).map(([key]) => [key, false]),
    ),
    bootstrap: [],
    ...over,
  };
}

/** Every account field label, for asserting which ones a page shows. */
export const ACCOUNT_FIELD_LABELS = ACCOUNT_FIELDS.map(([, label]) => label);
