import type { AdminSettingValue, AgentAdminSettings } from '@/lib/api/adminSettings';

/** Reads a setting as its editor shows it: an unsaved draft first, then the saved value. */
export type SettingReader = (key: string) => string;

const EMAIL_PROVIDER = 'accounts.email_provider';
const SMS_ENABLED = 'accounts.sms_enabled';
const SMS_PROVIDER = 'accounts.sms_provider';
const TENCENT_CREDENTIALS = new Set([
  'accounts.tencent_secret_id',
  'accounts.tencent_secret_key',
  'accounts.tencent_token',
]);

/**
 * Settings whose value decides which other settings apply. Their editors share
 * unsaved drafts with the page, so choosing a provider shows its fields at once,
 * including fields in another group (the shared Tencent Cloud credentials).
 */
export const GOVERNING_SETTINGS: ReadonlySet<string> = new Set([
  EMAIL_PROVIDER,
  SMS_ENABLED,
  SMS_PROVIDER,
]);

// Match the server's readers: delivery.py strips the email provider and treats
// empty as smtp; sms.py compares the SMS provider exactly.
function emailProvider(read: SettingReader): string {
  return read(EMAIL_PROVIDER).trim() || 'smtp';
}

function tencentSmsActive(read: SettingReader): boolean {
  return read(SMS_ENABLED) === 'true' && read(SMS_PROVIDER) === 'tencent';
}

/**
 * The setting to change before `key` applies, or `null` when it applies now.
 *
 * Fields of a provider that is not selected, or of phone sign-in while it is
 * off, are not shown, counted or searched; a link to one opens this setting
 * instead. Hidden values stay saved and are never written by hiding them.
 */
export function inactiveSettingGovernor(key: string, read: SettingReader): string | null {
  if (key.startsWith('accounts.smtp_')) {
    return emailProvider(read) === 'smtp' ? null : EMAIL_PROVIDER;
  }
  if (key.startsWith('accounts.tencent_ses_')) {
    return emailProvider(read) === 'tencent_ses' ? null : EMAIL_PROVIDER;
  }
  if (TENCENT_CREDENTIALS.has(key)) {
    return emailProvider(read) === 'tencent_ses' || tencentSmsActive(read) ? null : EMAIL_PROVIDER;
  }
  const smsSetting = key.startsWith('accounts.sms_') || key.startsWith('accounts.tencent_sms_');
  if (smsSetting && key !== SMS_ENABLED && read(SMS_ENABLED) !== 'true') return SMS_ENABLED;
  if (key.startsWith('accounts.tencent_sms_') && read(SMS_PROVIDER) !== 'tencent') {
    return SMS_PROVIDER;
  }
  return null;
}

/** Search words that lead to a governing setting while the fields it controls are hidden. */
export const GOVERNING_SEARCH_KEYWORDS: ReadonlyMap<string, string[]> = new Map([
  [EMAIL_PROVIDER, ['mail', 'SMTP', 'Tencent SES', 'Tencent Cloud credentials']],
  [SMS_ENABLED, ['SMS', 'text message', 'phone', 'Tencent SMS']],
  [SMS_PROVIDER, ['SMS', 'Tencent SMS']],
]);

/** A reader over the saved snapshot with unsaved governing drafts laid on top. */
export function settingReader(
  snapshot: AgentAdminSettings | null,
  drafts: Record<string, string>,
): SettingReader {
  return (key) => {
    if (drafts[key] !== undefined) return drafts[key];
    const value: AdminSettingValue | undefined =
      snapshot?.values[key] ??
      snapshot?.definitions.find((definition) => definition.key === key)?.default;
    return String(value ?? '');
  };
}
