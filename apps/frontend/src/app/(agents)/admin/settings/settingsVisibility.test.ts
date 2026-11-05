import { describe, expect, it } from 'vitest';

import { accountProviderSnapshot } from './settingsTestFixtures';
import { inactiveSettingGovernor, settingReader } from './settingsVisibility';

const SMTP = ['accounts.smtp_host', 'accounts.smtp_password', 'accounts.smtp_from'];
const SES = [
  'accounts.tencent_ses_region',
  'accounts.tencent_ses_from_email',
  'accounts.tencent_ses_template_verification',
];
const SMS = ['accounts.sms_provider', 'accounts.sms_code_ttl_s'];
const TENCENT_SMS = ['accounts.tencent_sms_app_id', 'accounts.tencent_sms_template_id'];
const CREDENTIALS = ['accounts.tencent_secret_id', 'accounts.tencent_secret_key'];
const ALWAYS = [
  'accounts.email_provider',
  'accounts.sms_enabled',
  'accounts.signup_enabled',
  'accounts.turnstile_site_key',
  'site.app_name',
];

function visible(values: Record<string, string | number | boolean>, drafts = {}) {
  const snapshot = accountProviderSnapshot(values);
  const read = settingReader(snapshot, drafts);
  return [...snapshot.definitions.map(({ key }) => key), 'site.app_name'].filter(
    (key) => inactiveSettingGovernor(key, read) === null,
  );
}

describe('provider-aware setting visibility', () => {
  it.each<{ case: string; values: Record<string, string | number | boolean>; shown: string[] }>([
    { case: 'SMTP mail, phone sign-in off', values: {}, shown: [...SMTP] },
    {
      case: 'an empty provider, which the server reads as SMTP',
      values: { 'accounts.email_provider': '  ' },
      shown: [...SMTP],
    },
    {
      case: 'Tencent SES mail',
      values: { 'accounts.email_provider': 'tencent_ses' },
      shown: [...SES, ...CREDENTIALS],
    },
    {
      case: 'Tencent SES with surrounding spaces',
      values: { 'accounts.email_provider': ' tencent_ses ' },
      shown: [...SES, ...CREDENTIALS],
    },
    {
      case: 'a registered mail provider',
      values: { 'accounts.email_provider': 'acme' },
      shown: [],
    },
    {
      case: 'phone sign-in on without a provider',
      values: { 'accounts.sms_enabled': true },
      shown: [...SMTP, ...SMS],
    },
    {
      case: 'phone sign-in on with a registered provider',
      values: { 'accounts.sms_enabled': true, 'accounts.sms_provider': 'acme-sms' },
      shown: [...SMTP, ...SMS],
    },
    {
      case: 'phone sign-in on with Tencent',
      values: { 'accounts.sms_enabled': true, 'accounts.sms_provider': 'tencent' },
      shown: [...SMTP, ...SMS, ...CREDENTIALS, ...TENCENT_SMS],
    },
    {
      case: 'phone sign-in off with a stale Tencent provider',
      values: { 'accounts.sms_provider': 'tencent', 'accounts.tencent_sms_app_id': '1400' },
      shown: [...SMTP],
    },
    {
      case: 'Tencent SES and Tencent SMS sharing credentials',
      values: {
        'accounts.email_provider': 'tencent_ses',
        'accounts.sms_enabled': true,
        'accounts.sms_provider': 'tencent',
      },
      shown: [...SES, ...SMS, ...CREDENTIALS, ...TENCENT_SMS],
    },
  ])('shows only what applies with $case', ({ values, shown }) => {
    expect(visible(values).sort()).toEqual([...ALWAYS, ...shown].sort());
  });

  it('lets unsaved drafts take precedence over saved values', () => {
    const saved = { 'accounts.email_provider': 'tencent_ses', 'accounts.sms_enabled': true };
    expect(visible(saved, { 'accounts.email_provider': 'smtp' })).toEqual(
      expect.arrayContaining(SMTP),
    );
    expect(visible(saved, { 'accounts.email_provider': 'smtp' })).not.toContain(
      'accounts.tencent_secret_id',
    );
    expect(visible(saved, { 'accounts.sms_provider': 'tencent' })).toEqual(
      expect.arrayContaining([...TENCENT_SMS, ...CREDENTIALS]),
    );
    expect(visible(saved, { 'accounts.sms_enabled': 'false' })).not.toContain(
      'accounts.sms_provider',
    );
  });

  it('names the switch or provider that would make a hidden field apply', () => {
    const read = settingReader(accountProviderSnapshot({ 'accounts.sms_provider': 'tencent' }), {});
    expect(inactiveSettingGovernor('accounts.tencent_ses_region', read)).toBe(
      'accounts.email_provider',
    );
    expect(inactiveSettingGovernor('accounts.tencent_secret_id', read)).toBe(
      'accounts.email_provider',
    );
    // Phone sign-in is off, so its switch comes before its provider.
    expect(inactiveSettingGovernor('accounts.tencent_sms_app_id', read)).toBe(
      'accounts.sms_enabled',
    );
    const enabled = settingReader(accountProviderSnapshot({ 'accounts.sms_enabled': true }), {});
    expect(inactiveSettingGovernor('accounts.tencent_sms_app_id', enabled)).toBe(
      'accounts.sms_provider',
    );
    expect(inactiveSettingGovernor('accounts.smtp_host', enabled)).toBeNull();
  });

  it('reads defaults for a value the snapshot omits and nothing for an unknown setting', () => {
    const snapshot = accountProviderSnapshot();
    delete snapshot.values['accounts.email_provider'];
    const read = settingReader(snapshot, {});
    expect(read('accounts.email_provider')).toBe('smtp');
    expect(read('accounts.unknown')).toBe('');
    expect(settingReader(null, {})('accounts.sms_enabled')).toBe('');
  });
});
