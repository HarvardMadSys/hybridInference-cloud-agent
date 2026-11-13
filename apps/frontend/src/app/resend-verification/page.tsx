import { EmailRequestForm } from '@/components/features/auth/EmailRequestForm';
import { LocalAccountGate } from '@/components/features/auth/LocalAccountGate';
export default function Page() {
  return (
    <LocalAccountGate>
      <EmailRequestForm purpose="verify" />
    </LocalAccountGate>
  );
}
