'use client';
import { useT } from '@/components/providers/LocaleProvider';
import { AccountField } from './AccountFrame';
import { PasswordInput, PasswordRules } from './PasswordInput';
export function NewPasswordFields({
  password,
  confirm,
  onPassword,
  onConfirm,
}: {
  password: string;
  confirm: string;
  onPassword: (v: string) => void;
  onConfirm: (v: string) => void;
}) {
  const t = useT();
  return (
    <>
      <AccountField id="password" label={t('account.password')}>
        <PasswordInput
          id="password"
          className="ui-input w-full"
          autoComplete="new-password"
          required
          minLength={8}
          maxLength={1024}
          aria-describedby="password-rules"
          value={password}
          onChange={(e) => onPassword(e.target.value)}
        />
        <div className="text-xs leading-5 text-muted" id="password-rules">
          <PasswordRules value={password} hint={t('account.passwordRules')} />
        </div>
      </AccountField>
      <AccountField id="confirm-password" label={t('account.confirmPassword')}>
        <PasswordInput
          id="confirm-password"
          className="ui-input w-full"
          autoComplete="new-password"
          required
          maxLength={1024}
          value={confirm}
          onChange={(e) => onConfirm(e.target.value)}
        />
      </AccountField>
    </>
  );
}
