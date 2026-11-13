import { LocalAccountGate } from '@/components/features/auth/LocalAccountGate';
import { TermsPage } from '@/components/features/auth/TermsPage';
export default function Page() {
  return (
    <LocalAccountGate>
      <TermsPage />
    </LocalAccountGate>
  );
}
