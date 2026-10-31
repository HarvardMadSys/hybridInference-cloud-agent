import { MailTokenForm } from '@/components/features/auth/MailTokenForm';
import { LocalAccountGate } from '@/components/features/auth/LocalAccountGate';
export default function Page() {
  return (
    <LocalAccountGate>
      <MailTokenForm purpose="reset" />
    </LocalAccountGate>
  );
}
