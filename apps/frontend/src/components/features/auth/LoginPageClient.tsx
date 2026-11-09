'use client';
import type { AuthConfig } from '@/lib/api/accounts';
import { Suspense } from 'react';
import { useAuthConfig } from '@/components/features/auth/useAuthConfig';
import { LocalLogin } from '@/components/features/auth/LocalLogin';
import { AccountNotice } from '@/components/features/auth/AccountFrame';
import { AuthFormFrame } from '@/components/features/auth/AuthFormFrame';
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
      <AuthFormFrame title={t('account.login')} description={t('account.loginDescription')}>
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
      </AuthFormFrame>
    );
  return (
    <Suspense fallback={null}>
      <LocalLogin signupEnabled={config.signup_enabled} />
    </Suspense>
  );
}
