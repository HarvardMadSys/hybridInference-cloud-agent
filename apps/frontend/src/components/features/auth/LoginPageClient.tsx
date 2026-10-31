'use client';
import type { AuthConfig } from '@/lib/api/accounts';
import { Suspense } from 'react';
import { useAuthConfig } from '@/components/features/auth/useAuthConfig';
import { LocalLogin } from '@/components/features/auth/LocalLogin';
import { AccountFrame, AccountNotice } from '@/components/features/auth/AccountFrame';
import { useT } from '@/components/providers/LocaleProvider';

export default function LoginPageClient({
  initialConfig = null,
}: {
  initialConfig?: AuthConfig | null;
}) {
  const { config, error, retry } = useAuthConfig(initialConfig);
  const t = useT();
  if (!config)
    return (
      <AccountFrame title={t('account.login')}>
        {error ? (
          <>
            <AccountNotice error>{error}</AccountNotice>
            <button className="ui-button ui-button-secondary" onClick={retry}>
              {t('common.retry')}
            </button>
          </>
        ) : (
          <p role="status">{t('common.loading')}</p>
        )}
      </AccountFrame>
    );
  return (
    <Suspense fallback={null}>
      <LocalLogin signupEnabled={config.signup_enabled} />
    </Suspense>
  );
}
