import { BrowserAccounts } from '@/components/features/auth/BrowserAccounts';
import { LocalAccountGate } from '@/components/features/auth/LocalAccountGate';
export default function Page() {
  return (
    <LocalAccountGate>
      <BrowserAccounts />
    </LocalAccountGate>
  );
}
