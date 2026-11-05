import { LocalAccountGate } from '@/components/features/auth/LocalAccountGate';
import { SignupPage } from '@/components/features/auth/SignupPage';
export default function Page() {
  return (
    <LocalAccountGate>
      <SignupPage />
    </LocalAccountGate>
  );
}
