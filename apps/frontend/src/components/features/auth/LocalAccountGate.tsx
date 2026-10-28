'use client';
import type { ReactNode } from 'react';
import { useT } from '@/components/providers/LocaleProvider';
import { useAuthConfig } from './useAuthConfig';
import { AccountFrame, AccountNotice, BackToLogin } from './AccountFrame';

export function LocalAccountGate({
  children,
  signup = false,
}: {
  children: ReactNode;
  signup?: boolean;
}) {
  const { config, error, retry } = useAuthConfig();
  const t = useT();
  if (error)
    return (
      <AccountFrame title={t('account.unavailable')}>
        <AccountNotice error>{error}</AccountNotice>
        <button className="ui-button ui-button-secondary" onClick={retry}>
          {t('common.retry')}
        </button>
      </AccountFrame>
    );
  if (!config)
    return (
      <AccountFrame title={t('account.login')}>
        <p role="status">{t('common.loading')}</p>
      </AccountFrame>
    );
  if (config.mode !== 'local' || (signup && !config.signup_enabled))
    return (
      <AccountFrame title={t('account.unavailable')}>
        <p className="text-sm text-muted">{t('account.registrationClosed')}</p>
        <BackToLogin />
      </AccountFrame>
    );
  return <>{children}</>;
}
