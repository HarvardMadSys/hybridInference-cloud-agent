import { SignupForm } from '@/components/features/auth/SignupForm';
import { LocalAccountGate } from '@/components/features/auth/LocalAccountGate';
export default function Page() {
  return (
    <LocalAccountGate signup>
      <SignupForm />
    </LocalAccountGate>
  );
}
